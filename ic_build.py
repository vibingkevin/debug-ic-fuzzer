"""执行单版构建，采集真实 konanc 调用、缓存和产物。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

from ic_config import Invalid, Project, absolute, within
from ic_results import Store, normalized, write_json


def option(args: list[str], *names: str) -> str | None:
    for i, arg in enumerate(args):
        if arg in names and i + 1 < len(args):
            return args[i + 1]
        for name in names:
            if arg.startswith(name + "="):
                return arg[len(name) + 1:]
    return None


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
