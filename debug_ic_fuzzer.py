"""逐提交回放 Native IC；运行入口见 README。"""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
import yaml


class Config(BaseModel):
    # 拼错字段直接报错，避免配置被静默忽略。
    model_config = ConfigDict(extra="forbid")


class Compiler(Config):
    kotlin_version: str
    kotlin_native_home: Path


class Compilers(Config):
    compilers: dict[Literal["baseline", "sut"], Compiler]


class Project(Config):
    repository: str
    branch: str
    start_commit: str
    end_commit: str
    direction: Literal["forward", "reverse"] = "forward"
    task: str
    gradle_args: list[str] = Field(default_factory=list)


class Projects(Config):
    projects: list[Project] = Field(min_length=1)


class Invalid(RuntimeError):
    pass


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commits(repo: Path, config: Project) -> list[str]:
    # 先确定含两端的主线区间，reverse 只改变执行顺序。
    history = git(repo, "rev-list", "--first-parent", "--reverse", f"refs/remotes/origin/{config.branch}").splitlines()
    start = git(repo, "rev-parse", "--verify", config.start_commit + "^{commit}")
    end = git(repo, "rev-parse", "--verify", config.end_commit + "^{commit}")
    if start not in history or end not in history or history.index(start) > history.index(end):
        raise Invalid("开始提交和结束提交必须在所选分支上，且开始提交不能晚于结束提交。")
    selected = history[history.index(start):history.index(end) + 1]
    return selected if config.direction == "forward" else selected[::-1]


def normalized(data: bytes) -> tuple[bytes, bool]:
    if b"\0" not in data:
        try:
            return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").strip().encode(), True
        except UnicodeDecodeError:
            pass
    return data, False


class Store:
    """同名文件按规范化内容去重；mtime 仍记录原文件的值。"""
    def __init__(self, root: Path):
        self.root = root

    def record(self, path: Path, save=False) -> dict:
        stat = path.stat()
        # IR 和二进制容器不能因为恰巧是 UTF-8 就按文本处理。
        binary = path.suffix in {".a", ".o", ".so", ".dylib", ".klib", ".kexe", ".bc", ".jar"} or "ir" in path.parts
        if binary:
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "md5").hexdigest()
            content, is_text = None, False
        else:
            content, is_text = normalized(path.read_bytes())
            digest = hashlib.md5(content).hexdigest()
        result = dict(path=str(path), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                      md5=digest, text=is_text)
        if save:
            target = self.root / path.name / f"{path.name}_{digest}"
            target.parent.mkdir(parents=True, exist_ok=True)
            # 保存时即去重，不硬链接仍会被编译器改写的工作缓存。
            if not target.exists():
                if content is None:
                    shutil.copyfile(path, target)
                else:
                    target.write_bytes(content)
            result["stored"] = str(target.relative_to(self.root.parent))
        return result


def option(args: list[str], *names: str) -> str | None:
    for i, arg in enumerate(args):
        if arg in names and i + 1 < len(args):
            return args[i + 1]
        for name in names:
            if arg.startswith(name + "="):
                return arg[len(name) + 1:]
    return None


def absolute(value: str, cwd: Path) -> Path:
    return (cwd / value).resolve()


def within(path: Path, roots: list[Path]) -> bool:
    return any(path.resolve().is_relative_to(root.resolve()) for root in roots)


def argfile_record(path: Path) -> dict:
    data = path.read_bytes()
    return {"base64": base64.b64encode(data).decode(), "md5": hashlib.md5(normalized(data)[0]).hexdigest(),
            "size": len(data), "mtime_ns": path.stat().st_mtime_ns}


class Capture:
    """读取 KGP 的真实调用日志；不是通过任务属性重建一份近似参数。"""
    def __init__(self, cwd: Path, home: Path, step: Path):
        self.cwd, self.home, self.step = cwd, home, step
        self.calls, self.context = [], []
        self.field, self.items = None, []

    def line(self, line: str):
        line = line.rstrip("\n")
        if line.startswith('Run in-process tool ') or line.startswith('Run "'):
            self.context = [line]
        elif self.context:
            self.context.append(line)
        if line in {"Arguments = [", "Transformed arguments = ["}:
            self.field, self.items = line.split(" = ")[0], []
        elif self.field and line == "]":
            if self.field == "Arguments":
                self.calls.append(dict(argv=self.items, cwd=str(self.cwd), launcher=list(self.context), argfiles={}))
            if self.calls:
                prefixes = ("@", "-Xexternal-dependencies=") if self.field == "Arguments" else ("@",)
                for arg in self.items:
                    prefix = next((p for p in prefixes if arg.startswith(p)), None)
                    if prefix is not None:
                        path = absolute(arg[len(prefix):], self.cwd)
                        if path.is_file():
                            self.calls[-1]["argfiles"][str(path)] = argfile_record(path)
            self.field = None
        elif self.field:
            value = line.strip()
            self.items.append(json.loads(value) if value.startswith('"') else value)

    def finish(self):
        for call in self.calls:
            call["konanc"] = shlex.join([str(self.home / "bin/konanc"), *call["argv"]])
        write_json(self.step / "commands.json", self.calls)


def prepare_home(source: Path, temporary: ExitStack) -> Path:
    if source.is_file() and (source.name.endswith(".tgz") or source.name.endswith(".tar.gz")):
        # 即使两版配置同一个发行包，也各自解压，防止共享可写缓存。
        folder = Path(temporary.enter_context(tempfile.TemporaryDirectory(prefix="kn-ic-eval-", dir="/tmp")))
        with tarfile.open(source) as archive:
            archive.extractall(folder, filter="data")
        candidates = list(folder.glob("*/bin/konanc")) + list(folder.glob("bin/konanc"))
        if len(candidates) != 1:
            raise Invalid(f"发行包中必须有唯一 bin/konanc：{source}")
        return candidates[0].parent.parent.resolve()
    if not (source / "bin/konanc").is_file():
        raise Invalid(f"无效 kotlin_native_home：{source}")
    return source.resolve()


def init_script() -> Path:
    local = Path(__file__).with_name("capture.init.gradle")
    return local if local.exists() else Path(sys.prefix) / "share/debug-ic-fuzzer/capture.init.gradle"


def files_under(path: Path):
    return [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())


class Variant:
    """一版编译器的独立 checkout、缓存根和上一笔成功快照。"""
    def __init__(self, name, checkout, home, compiler, store):
        self.name, self.checkout, self.home = name, checkout, home
        self.compiler, self.store = compiler, store
        self.gradle = checkout.parent / (name + "-gradle")
        self.ic_roots, self.output_roots, self.klib_roots = set(), set(), set()
        self.previous = None

    def build(self, project: Project, step: Path, clean: bool) -> dict:
        step.mkdir(parents=True)
        if clean:
            # 只删除本工具登记过的工程输出和 IC；首次 clone 没有工程缓存。
            for root in self.ic_roots | self.output_roots:
                if not root.is_relative_to(self.checkout) or root == self.checkout:
                    raise Invalid(f"拒绝清理工程外目录：{root}")
                if root.exists():
                    shutil.rmtree(root)
            self.previous = None
        env = os.environ | {"GRADLE_USER_HOME": str(self.gradle), "IC_EVAL_KOTLIN": self.compiler.kotlin_version,
                            "IC_EVAL_HOME": str(self.home), "IC_EVAL_STEP": str(step)}
        # 禁用配置缓存，确保每轮注入新的采集目录。
        # 普通格式的详细日志用于提取真实 konanc 参数；运行资源设置沿用项目默认值。
        command = [str(self.checkout / "gradlew"), project.task, *project.gradle_args,
                   "--no-configuration-cache", "--console=plain", "--info",
                   "-I", str(init_script()), "-Pkotlin.incremental.native=true",
                   "-Pkotlin.internal.compiler.arguments.log.level=info", f"-Pkotlin.native.home={self.home}"]
        write_json(step / "build.json", dict(command=command, cwd=str(self.checkout), clean=clean,
                   kotlin_version=self.compiler.kotlin_version, native_home=str(self.home)))
        capture = Capture(self.checkout, self.home, step)
        started = time.monotonic()
        with (step / "gradle.log").open("w") as log:
            with subprocess.Popen(command, cwd=self.checkout, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, errors="replace") as process:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    capture.line(line)
                code = process.wait()
        capture.finish()
        seconds = time.monotonic() - started
        errors, rebuilt, inputs = [], set(), {}
        links = 0
        for call in capture.calls:
            args = call["argv"]
            inputs.update({str(p): self.store.record(p)
                for flag, value in zip(args, args[1:]) if flag in {"-library", "-l", "-include", "-native-library"}
                for p in files_under(absolute(value, self.checkout))})
            output = option(args, "-output", "-o")
            parent = absolute(output, self.checkout).parent if output else None
            local_output = parent is not None and parent != self.checkout and parent.is_relative_to(self.checkout)
            if option(args, "-produce", "-p") == "library" and local_output:
                self.klib_roots.add(parent)
            ic = option(args, "-Xic-cache-dir")
            if not ic:
                continue
            links += 1
            ic = absolute(ic, self.checkout)
            if "-opt" in args or "-g" not in args or "-Xenable-incremental-compilation" not in args:
                errors.append("不是 Debug IC 调用")
            if not ic.is_relative_to(self.checkout) or ic == self.checkout:
                errors.append(f"工程 IC 根越界：{ic}")
                continue
            self.ic_roots.add(ic)
            if local_output:
                self.output_roots.add(parent)
            else:
                errors.append(f"输出目录越界：{absolute(output, self.checkout)}" if output else "缺少 -output/-o")
            dump = option(args, "-Xdump-built-caches-to")
            if dump and Path(dump).exists():
                rebuilt.update(line for line in Path(dump).read_text().splitlines() if line.strip())
            elif code == 0:
                errors.append("编译成功但缺少重编清单")
        if not links and code == 0:
            errors.append("没有实际执行启用 IC 的 Native 链接")
        # 只信任已登记的根；不能根据越界的输出反过来扩大跟踪范围。
        roots = [*self.ic_roots, self.home / "klib/cache"]
        for path in sorted(rebuilt):
            if not within(Path(path), roots):
                errors.append(f"CACHE_OUT_OF_SCOPE: {path}")
            elif code == 0 and not Path(path).is_file():
                errors.append(f"重编清单中的归档不存在：{path}")
        for task_file in step.glob("*.task.json"):
            task = json.loads(task_file.read_text())
            if code == 0 and not Path(task["output"]).exists():
                errors.append(f"Native 输出不存在：{task['output']}")
        status = "INVALID" if errors else "BUILD_FAIL" if code else "PASS"
        files = self.collect()
        keys = {v["path"]: k for k, v in files.items()}
        previous = self.previous if status == "PASS" else None
        same = {k for k, v in files.items() if v["md5"] == (previous or {}).get(k, {}).get("md5")}
        # 重编事实取编译器清单；MD5 只回答重编后内容是否变化。
        result = dict(variant=self.name, clean=clean, exit_code=code, seconds=seconds,
            status=status, rebuilt=sorted(rebuilt), errors=errors, calls=capture.calls, files=files, inputs=inputs,
            rebuilt_keys=[k for k, v in files.items() if v["path"] in rebuilt],
            unchanged_rebuilt=[keys[p] for p in sorted(rebuilt) if keys.get(p) in same] if previous is not None else None,
            retained=[k for k, v in files.items() if k in same and k.endswith(".a") and v["path"] not in rebuilt])
        self.previous = files if status == "PASS" else None
        result["evidence"] = {p.name: self.store.record(p, save=True) for p in step.iterdir() if p.is_file()}
        for record in result["evidence"].values():
            Path(record["path"]).unlink()
        write_json(step / "result.json", result)
        return result

    def collect(self) -> dict:
        """只做后采集；诊断文件保存内容，但不作为跨版本相等条件。"""
        files = {}
        roots = [("project", self.checkout, [p for p in self.checkout.rglob("build") if p.is_dir() and ".git" not in p.parts]),
                 ("compiler-cache", self.home / "klib/cache", [self.home / "klib/cache"])]
        for label, base, scan in roots:
            for path in sorted({p for root in scan for p in files_under(root)}):
                cache = label == "compiler-cache" or within(path, list(self.ic_roots))
                consumed = path.suffix in {".a", ".h"} or "ir" in path.parts or path.name in {"metadata.properties", "bitcode_deps"}
                compare = within(path, [*self.output_roots, *self.klib_roots]) or (cache and consumed)
                save = path.name in {"metadata.properties", "bitcode_deps"} or path.suffix in {".h", ".json", ".log", ".txt"} or "digest" in path.name
                files[f"{label}/{path.relative_to(base)}"] = self.store.record(path, save) | {"compare": compare}
        return files

    def preserve(self, result: dict):
        for record in result["files"].values():
            if "stored" not in record:
                path = Path(record["path"])
                if path.is_file():
                    saved = self.store.record(path, True)
                    if saved["md5"] != record["md5"]:
                        raise Invalid(f"采集后文件又变化：{path}")
                    record.update(saved)


def comparable(record: dict, store: Store):
    name = Path(record["path"]).name
    if name == "metadata.properties" and "stored" in record:
        text = (store.root.parent / record["stored"]).read_text()
        return sorted(line for line in text.splitlines() if not line.startswith("compilerFingerprint="))
    if name == "bitcode_deps" and "stored" in record:
        return sorted(set((store.root.parent / record["stored"]).read_text().splitlines()))
    return record["md5"]


def differences(left: dict, right: dict, store: Store) -> list[str]:
    a = {k: v for k, v in left["files"].items() if v["compare"]}
    b = {k: v for k, v in right["files"].items() if v["compare"]}
    return [key for key in sorted(a.keys() | b.keys())
            if key not in a or key not in b or comparable(a[key], store) != comparable(b[key], store)]


def pair_status(results, store):
    if len(results) != 2 or any(r["status"] == "INVALID" for r in results):
        return "INVALID", []
    if any(r["status"] == "BUILD_FAIL" for r in results):
        return "BUILD_FAIL", []
    diff = differences(*results, store)
    return ("DIFF" if diff else "PASS"), diff


def replay(project, variants, revisions, destination, store):
    rows, failed = [], False
    for index, revision in enumerate(revisions):
        for variant in variants:
            git(variant.checkout, "checkout", "--detach", revision)
        folder = destination / f"{index:04d}-{revision[:12]}"
        row = dict(commit=revision, steps=[])
        rows.append(row)
        previous = None
        last = index == len(revisions) - 1
        for phase in (["clean"] if index == 0 else ["incremental", "clean"]):
            if previous and row["steps"][-1]["status"] == "PASS" and not last:
                break
            results = []
            for variant in variants:
                results.append(variant.build(project, folder / phase / variant.name, phase == "clean"))
                if results[-1]["status"] == "INVALID":
                    break
            status, diff = pair_status(results, store)
            step = dict(phase=phase, status=status, diff=diff)
            changed_after_clean = False
            if len(results) == 2:
                if previous:
                    step["incremental_vs_clean"] = {v.name: differences(old, new, store)
                        for v, old, new in zip(variants, previous, results) if old["status"] == "PASS"}
                    changed_after_clean = any(step["incremental_vs_clean"].values())
                else:
                    step["skipped_by_sut"] = sorted(set(results[0].get("rebuilt_keys", [])) & set(results[1].get("retained", [])))
                    step["recompilation"] = {v.name: {"rebuilt": len(r.get("rebuilt", [])),
                        "unchanged": r.get("unchanged_rebuilt")} for v, r in zip(variants, results)}
            # 清理前先留证；clean 恢复成功也不能抹掉已经观察到的异常。
            failed |= status != "PASS" or changed_after_clean
            if status != "PASS" or changed_after_clean or (last and previous is None):
                for variant, result in zip(variants, results):
                    variant.preserve(result)
                    write_json(folder / phase / variant.name / "result.json", result)
            row["steps"].append(step)
            write_json(destination / "summary.json", rows)
            print(f"{revision[:12]} {phase}: {status}", flush=True)
            if status == "INVALID" or (phase == "clean" and status != "PASS"):
                return 2 if status == "INVALID" else 1
            previous = results
    return int(failed)


def run(compilers_path: Path, projects_path: Path, destination: Path) -> int:
    compilers = Compilers.model_validate(yaml.safe_load(compilers_path.read_text())).compilers
    if set(compilers) != {"baseline", "sut"}:
        raise Invalid("compilers 必须同时提供 baseline 和 sut")
    projects = Projects.model_validate(yaml.safe_load(projects_path.read_text())).projects
    destination.mkdir(parents=True, exist_ok=False)
    write_json(destination / "config.json", dict(compilers={k: v.model_dump(mode="json") for k, v in compilers.items()},
                                               projects=[p.model_dump() for p in projects]))
    with ExitStack() as temporary:
        homes = {name: prepare_home(absolute(str(c.kotlin_native_home), compilers_path.parent), temporary)
                 for name, c in compilers.items()}
        if homes["baseline"] == homes["sut"] or any(
                a.is_relative_to(b) for a, b in [(homes["baseline"], homes["sut"]), (homes["sut"], homes["baseline"])]):
            raise Invalid("baseline 和 sut 必须使用不同的编译器目录")
        store = Store(destination / "store")
        if homes["baseline"].joinpath("klib/cache").resolve() == homes["sut"].joinpath("klib/cache").resolve():
            raise Invalid("baseline 和 sut 不能共用同一个编译缓存目录")
        write_json(destination / "toolchains.json", {name: {
            "home": str(home), "version": subprocess.check_output([str(home / "bin/konanc"), "-version"],
                stderr=subprocess.STDOUT, text=True),
            "compiler_jars": {str(p.relative_to(home)): store.record(p) for p in sorted((home / "konan/lib").glob("*.jar"))}
        } for name, home in homes.items()})
        for index, project in enumerate(projects):
            root = destination / f"project-{index}"
            root.mkdir()
            repo = project.repository
            if (projects_path.parent / repo).exists():
                repo = str((projects_path.parent / repo).resolve())
            subprocess.run(["git", "clone", repo, str(root / "baseline")], check=True)
            revisions = commits(root / "baseline", project)
            git(root / "baseline", "worktree", "add", "--detach", str(root / "sut"), revisions[0])
            variants = [Variant(name, root / name, homes[name], compilers[name], store) for name in ("baseline", "sut")]
            write_json(root / "commits.json", revisions)
            status = replay(project, variants, revisions, root / "results", store)
            if status:
                return status
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compilers", required=True, type=Path)
    parser.add_argument("--projects", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path, help="保存结果的目录，必须尚未创建")
    args = parser.parse_args()
    if args.run_dir.exists():
        parser.error("run-dir 已存在，请使用新目录")
    try:
        return run(args.compilers.resolve(), args.projects.resolve(), args.run_dir.resolve())
    except (Invalid, ValueError, OSError, yaml.YAMLError, tarfile.TarError, subprocess.CalledProcessError) as error:
        write_json(args.run_dir / "error.json", dict(status="INVALID", error=str(error)))
        print(f"INVALID: {error}", file=sys.stderr)
        return 2



if __name__ == "__main__":
    sys.exit(main())
