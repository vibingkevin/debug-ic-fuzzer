"""配置校验、路径边界和编译器准备。"""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import os
import platform
import tarfile
import tempfile
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Config(BaseModel):
    # 拼错字段直接报错，避免配置被静默忽略。
    model_config = ConfigDict(extra="forbid")


class Compiler(Config):
    kotlin_version: str
    kotlin_native_home: Path | None = None


class Compilers(Config):
    compilers: dict[Literal["baseline", "sut"], Compiler]


class Project(Config):
    repository: str = Field(pattern=r"^(?:https?://|ssh://|git@[^:]+:)")
    branch: str
    start_commit: str
    end_commit: str
    direction: Literal["forward", "reverse"] = "forward"
    task: str = Field(pattern=r"^:[^\s:]+(?::[^\s:]+)*$")
    gradle_args: list[str] = Field(default_factory=list)


class Invalid(RuntimeError):
    pass


def absolute(value: str, cwd: Path) -> Path:
    return (cwd / value).resolve()


def expand_args(args: list[str], base: Path) -> list[str]:
    return [arg.replace("{config_dir}", str(base.resolve())) for arg in args]


def within(path: Path, roots: list[Path]) -> bool:
    return any(path.resolve().is_relative_to(root.resolve()) for root in roots)


def default_home(version: str) -> Path:
    # 与 KGP 的 prebuilt 目录命名一致；尚未下载时也返回路径，由 KGP 下载。
    host = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}[platform.system()]
    arch = platform.machine().lower()
    arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(arch, arch)
    root = Path(os.environ.get("KONAN_DATA_DIR", str(Path.home() / ".konan")))
    return (root / f"kotlin-native-prebuilt-{host}-{arch}-{version}").resolve()


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
