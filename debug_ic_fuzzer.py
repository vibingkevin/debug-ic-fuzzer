"""逐提交回放 Native IC；运行入口见 README。"""
from __future__ import annotations

import argparse
import base64
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
    history = git(repo, "rev-list", "--first-parent", "--reverse", config.branch).splitlines()
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
    path = Path(value)
    return (cwd / path).resolve() if not path.is_absolute() else path.resolve()


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
                files = {}
                for arg in self.items:
                    if arg.startswith("-Xexternal-dependencies=") or arg.startswith("@"):
                        value = arg.split("=", 1)[1] if "=" in arg else arg[1:]
                        path = absolute(value, self.cwd)
                        if path.is_file():
                            files[str(path)] = argfile_record(path)
                self.calls.append(dict(argv=self.items, cwd=str(self.cwd), launcher=list(self.context), argfiles=files))
            elif self.calls:
                for arg in self.items:
                    if arg.startswith("@"):
                        path = absolute(arg[1:], self.cwd)
                        if path.exists():
                            self.calls[-1]["argfiles"][str(path)] = argfile_record(path)
            self.field = None
        elif self.field:
            value = line.strip()
            self.items.append(json.loads(value) if value.startswith('"') else value)

    def finish(self):
        for call in self.calls:
            call["konanc"] = shlex.join([str(self.home / "bin/konanc"), *call["argv"]])
        write_json(self.step / "commands.json", self.calls)


def prepare_home(source: Path, temporary: list[tempfile.TemporaryDirectory]) -> Path:
    if source.is_file() and (source.name.endswith(".tgz") or source.name.endswith(".tar.gz")):
        # 即使两版配置同一个发行包，也各自解压，防止共享可写缓存。
        temp = tempfile.TemporaryDirectory(prefix="kn-ic-eval-", dir="/tmp")
        temporary.append(temp)
        with tarfile.open(source) as archive:
            archive.extractall(temp.name, filter="data")
        candidates = list(Path(temp.name).glob("*/bin/konanc")) + list(Path(temp.name).glob("bin/konanc"))
        if len(candidates) != 1:
            raise Invalid(f"发行包中必须有唯一 bin/konanc：{source}")
        return candidates[0].parent.parent.resolve()
    if not (source / "bin/konanc").is_file():
        raise Invalid(f"无效 kotlin_native_home：{source}")
    return source.resolve()


def init_script() -> Path:
    local = Path(__file__).with_name("capture.init.gradle")
    return local if local.exists() else Path(sys.prefix) / "share/debug-ic-fuzzer/capture.init.gradle"


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
        command = [str(self.checkout / "gradlew"), project.task,
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
        result = dict(variant=self.name, clean=clean, exit_code=code, seconds=time.monotonic() - started,
                      status="BUILD_FAIL" if code else "PASS", rebuilt=[], unchanged_rebuilt=None, retained=[],
                      files={}, errors=[], calls=capture.calls)
        for call in capture.calls:
            if option(call["argv"], "-produce", "-p") == "library":
                output = option(call["argv"], "-output", "-o")
                if output:
                    parent = absolute(output, self.checkout).parent
                    if parent.is_relative_to(self.checkout) and parent != self.checkout:
                        self.klib_roots.add(parent)
        links = [c for c in capture.calls if option(c["argv"], "-Xic-cache-dir")]
        if not links and code == 0:
            result["errors"].append("没有实际执行启用 IC 的 Native 链接")
        for call in links:
            args = call["argv"]
            ic = absolute(option(args, "-Xic-cache-dir"), self.checkout)
            output = option(args, "-output", "-o")
            if "-opt" in args or "-g" not in args or "-Xenable-incremental-compilation" not in args:
                result["errors"].append("不是 Debug IC 调用")
            if not ic.is_relative_to(self.checkout) or ic == self.checkout:
                result["errors"].append(f"工程 IC 根越界：{ic}")
                continue
            self.ic_roots.add(ic)
            if output:
                output = absolute(output, self.checkout)
                if output.parent.is_relative_to(self.checkout) and output.parent != self.checkout:
                    self.output_roots.add(output.parent)
                else:
                    result["errors"].append(f"输出目录越界：{output}")
            else:
                result["errors"].append("缺少 -output/-o")
            dump = option(args, "-Xdump-built-caches-to")
            if dump and Path(dump).exists():
                result["rebuilt"].extend(line for line in Path(dump).read_text().splitlines() if line.strip())
            elif code == 0:
                result["errors"].append("编译成功但缺少重编清单")
        # 只信任已登记的根；不能根据越界的输出反过来扩大跟踪范围。
        roots = [*self.ic_roots, self.home / "klib/cache"]
        for path in sorted(set(result["rebuilt"])):
            if not within(Path(path), roots):
                result["errors"].append(f"CACHE_OUT_OF_SCOPE: {path}")
            elif code == 0 and not Path(path).is_file():
                result["errors"].append(f"重编清单中的归档不存在：{path}")
        for task_file in step.glob("*.task.json"):
            task = json.loads(task_file.read_text())
            if code == 0 and not Path(task["output"]).exists():
                result["errors"].append(f"Native 输出不存在：{task['output']}")
        result["rebuilt"] = sorted(set(result["rebuilt"]))
        result["files"] = self.collect()
        result["inputs"] = {}
        for call in capture.calls:
            args = call["argv"]
            for i, arg in enumerate(args[:-1]):
                if arg not in {"-library", "-l", "-include", "-native-library"}:
                    continue
                path = absolute(args[i + 1], self.checkout)
                for source in ([path] if path.is_file() else sorted(path.rglob("*"))):
                    if source.is_file():
                        result["inputs"][str(source)] = self.store.record(source)
        if result["errors"]:
            result["status"] = "INVALID"
        # 重编事实取编译器清单；MD5 只回答重编后内容是否变化。
        if result["status"] == "PASS" and self.previous is not None:
            current = {v["path"]: (k, v) for k, v in result["files"].items()}
            result["unchanged_rebuilt"] = [current[p][0] for p in result["rebuilt"] if p in current
                and current[p][0] in self.previous and current[p][1]["md5"] == self.previous[current[p][0]]["md5"]]
            result["retained"] = [k for k, v in result["files"].items() if k.endswith(".a")
                and v["path"] not in result["rebuilt"] and k in self.previous and v["md5"] == self.previous[k]["md5"]]
        result["rebuilt_keys"] = [k for k, v in result["files"].items() if v["path"] in result["rebuilt"]]
        self.previous = result["files"] if result["status"] == "PASS" else None
        result["evidence"] = {}
        for path in list(step.iterdir()):
            if path.is_file():
                result["evidence"][path.name] = self.store.record(path, save=True)
                path.unlink()
        write_json(step / "result.json", result)
        return result

    def collect(self) -> dict:
        """只做后采集；诊断文件保存内容，但不作为跨版本相等条件。"""
        files = {}
        roots = [("project", self.checkout, [p for p in self.checkout.rglob("build") if p.is_dir() and ".git" not in p.parts]),
                 ("compiler-cache", self.home / "klib/cache", [self.home / "klib/cache"])]
        for label, base, scan in roots:
            for root in scan:
                if not root.exists():
                    continue
                for path in sorted(root.rglob("*")):
                    if not path.is_file():
                        continue
                    key = f"{label}/{path.relative_to(base)}"
                    if key in files:
                        continue
                    cache = label == "compiler-cache" or within(path, list(self.ic_roots))
                    consumed = path.suffix in {".a", ".h"} or "ir" in path.parts or path.name in {"metadata.properties", "bitcode_deps"}
                    compare = within(path, [*self.output_roots, *self.klib_roots]) or (cache and consumed)
                    save = path.name in {"metadata.properties", "bitcode_deps"} or path.suffix in {".h", ".json", ".log", ".txt"} or "digest" in path.name
                    files[key] = self.store.record(path, save)
                    files[key]["compare"] = compare
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
    if any(r["status"] == "INVALID" for r in results):
        return "INVALID", []
    if any(r["status"] == "BUILD_FAIL" for r in results):
        return "BUILD_FAIL", []
    diff = differences(*results, store)
    return ("DIFF" if diff else "PASS"), diff


def replay(project, variants, revisions, destination, store):
    rows, any_failure = [], False
    for index, revision in enumerate(revisions):
        for variant in variants:
            git(variant.checkout, "checkout", "--detach", revision)
        folder = destination / f"{index:04d}-{revision[:12]}"
        row = dict(commit=revision, steps=[])
        clean = index == 0
        results = []
        phase = "clean" if clean else "incremental"
        for variant in variants:
            result = variant.build(project, folder / phase / variant.name, clean)
            results.append(result)
            if result["status"] == "INVALID":
                break
        status, diff = pair_status(results, store) if len(results) == 2 else ("INVALID", [])
        step = dict(phase=phase, status=status, diff=diff)
        if len(results) == 2:
            step["skipped_by_sut"] = sorted(set(results[0].get("rebuilt_keys", [])) & set(results[1].get("retained", [])))
            step["recompilation"] = {v.name: {"rebuilt": len(r.get("rebuilt", [])),
                "unchanged": r.get("unchanged_rebuilt")} for v, r in zip(variants, results)}
        row["steps"].append(step)
        print(f"{revision[:12]} {phase}: {status}", flush=True)
        # clean 恢复后可继续，但不能抹掉已经观察到的异常。
        any_failure |= status != "PASS"
        if status != "PASS" or index == len(revisions) - 1:
            for variant, result in zip(variants, results):
                variant.preserve(result)
                write_json(folder / phase / variant.name / "result.json", result)
        if status == "INVALID" or (clean and status != "PASS"):
            rows.append(row)
            write_json(destination / "summary.json", rows)
            return False
        if not clean and (status != "PASS" or index == len(revisions) - 1):
            clean_results = []
            for variant in variants:
                result = variant.build(project, folder / "clean" / variant.name, True)
                clean_results.append(result)
                if result["status"] == "INVALID":
                    break
            clean_status, clean_diff = pair_status(clean_results, store) if len(clean_results) == 2 else ("INVALID", [])
            recovery = dict(phase="clean", status=clean_status, diff=clean_diff)
            if len(clean_results) == 2:
                recovery["incremental_vs_clean"] = {v.name: differences(old, new, store)
                    for v, old, new in zip(variants, results, clean_results) if old["status"] == "PASS"}
                changed_after_clean = any(recovery["incremental_vs_clean"].values())
                any_failure |= changed_after_clean
                if changed_after_clean:
                    for variant, result in zip(variants, clean_results):
                        variant.preserve(result)
                        write_json(folder / "clean" / variant.name / "result.json", result)
            row["steps"].append(recovery)
            print(f"{revision[:12]} clean: {clean_status}", flush=True)
            if clean_status != "PASS":
                for variant, result in zip(variants, clean_results):
                    variant.preserve(result)
                    write_json(folder / "clean" / variant.name / "result.json", result)
                rows.append(row)
                write_json(destination / "summary.json", rows)
                return False
        rows.append(row)
        write_json(destination / "summary.json", rows)
    return not any_failure


def run(compilers_path: Path, projects_path: Path, destination: Path) -> bool:
    compilers = Compilers.model_validate(yaml.safe_load(compilers_path.read_text())).compilers
    if set(compilers) != {"baseline", "sut"}:
        raise Invalid("compilers 必须同时提供 baseline 和 sut")
    projects = Projects.model_validate(yaml.safe_load(projects_path.read_text())).projects
    destination.mkdir(parents=True, exist_ok=False)
    write_json(destination / "config.json", dict(compilers={k: v.model_dump(mode="json") for k, v in compilers.items()},
                                               projects=[p.model_dump() for p in projects]))
    temporary = []
    try:
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
        all_passed = True
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
            passed = replay(project, variants, revisions, root / "results", store)
            all_passed &= passed
            if not passed:
                break
        return all_passed
    finally:
        for temp in temporary:
            temp.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compilers", required=True, type=Path)
    parser.add_argument("--projects", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path, help="保存结果的目录，必须尚未创建")
    args = parser.parse_args()
    if args.run_dir.exists():
        parser.error("run-dir 已存在，请使用新目录")
    try:
        passed = run(args.compilers.resolve(), args.projects.resolve(), args.run_dir.resolve())
    except (Invalid, ValueError, OSError, yaml.YAMLError, tarfile.TarError, subprocess.CalledProcessError) as error:
        write_json(args.run_dir / "error.json", dict(status="INVALID", error=str(error)))
        print(f"INVALID: {error}", file=sys.stderr)
        return 2
    if passed:
        return 0
    for summary in args.run_dir.glob("project-*/results/summary.json"):
        if any(s["status"] == "INVALID" for row in json.loads(summary.read_text()) for s in row["steps"]):
            return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
