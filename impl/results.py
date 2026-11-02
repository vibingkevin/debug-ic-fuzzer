"""文件快照、内容去重和结果比较。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


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
