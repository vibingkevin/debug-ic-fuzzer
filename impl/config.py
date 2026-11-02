"""Load configuration and resolve both Native homes."""

from contextlib import ExitStack
from pathlib import Path
import os
import platform
import tarfile
import tempfile
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Compiler(Config):
    kotlin_version: str
    kotlin_native_home: Path | None = None


class Compilers(Config):
    compilers: dict[Literal["baseline", "sut"], Compiler]

    @model_validator(mode="after")
    def pair(self):
        if set(self.compilers) != {"baseline", "sut"}:
            raise ValueError("compilers must define both baseline and sut")
        return self


class Project(Config):
    repository: str = Field(pattern=r"^(?:https?://|ssh://|git@[^:]+:)")
    branch: str
    start_commit: str
    end_commit: str
    direction: Literal["forward", "reverse"] = "forward"
    task: str = Field(pattern=r"^:[^\s:]+(?::[^\s:]+)*$")
    gradle_args: list[str] = Field(default_factory=list)


def load(path: Path, model):
    return model.model_validate(yaml.safe_load(path.read_text()))


def prepare_home(compiler: Compiler, config_dir: Path, temporary: ExitStack) -> Path:
    host = {
        ("Darwin", "arm64"): "macos-aarch64",
        ("Linux", "x86_64"): "linux-x86_64",
    }.get((platform.system(), platform.machine()))
    if host is None:
        raise ValueError("Only Apple Silicon Mac and Linux x64 are supported")
    native_home_path = compiler.kotlin_native_home
    if native_home_path is None:
        # Resolve the path for the sharing check; let KGP download without passing native.home.
        base = Path(os.environ.get("KONAN_DATA_DIR", str(Path.home() / ".konan")))
        return (
            base / f"kotlin-native-prebuilt-{host}-{compiler.kotlin_version}"
        ).resolve()
    if native_home_path.name.endswith((".tgz", ".tar.gz")):
        extraction_dir = Path(
            temporary.enter_context(
                tempfile.TemporaryDirectory(prefix="ic-fuzzer-", dir="/tmp")
            )
        )
        archive_path = config_dir / native_home_path
        with tarfile.open(archive_path) as archive:
            archive.extractall(extraction_dir, filter="data")
        candidates = list(extraction_dir.glob("*/bin/konanc")) + list(extraction_dir.glob("bin/konanc"))
        if len(candidates) != 1:
            raise ValueError(
                f"The archive must contain exactly one bin/konanc: {native_home_path}"
            )
        return candidates[0].parent.parent.resolve()
    if not native_home_path.is_absolute():
        raise ValueError(f"Compiler directory must be an absolute path: {native_home_path}")
    if not (native_home_path / "bin/konanc").is_file():
        raise ValueError(f"Cannot find bin/konanc: {native_home_path}")
    return native_home_path.resolve()
