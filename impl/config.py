"""Load configuration and resolve both Native homes."""

from contextlib import ExitStack
from pathlib import Path
import platform
import tarfile
import tempfile
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Compiler(Config):
    kotlin_version: str
    kotlin_native_home: Path | list[Path] | None = None


class Compilers(Config):
    compilers: dict[Literal["baseline", "sut"], Compiler]

    @model_validator(mode="after")
    def validate_compiler_pair(self):
        """Validate this config's compiler names are exactly baseline and sut; return self."""
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


def load_config(config_path: Path, config_model):
    """Read config_path and validate it with the supplied Pydantic config_model class.

    Return the model instance; file, YAML, and validation errors propagate.
    """
    return config_model.model_validate(yaml.safe_load(config_path.read_text()))


def resolve_native_home(compiler_config: Compiler, compiler_config_directory: Path, extraction_cleanup: ExitStack) -> Path:
    """Resolve an absolute Native home, extracting archives into fresh temporary storage.

    Args:
        compiler_config: Kotlin version and optional distribution directory or archive list.
        compiler_config_directory: Compiler YAML directory used to resolve relative archive paths.
        extraction_cleanup: ExitStack that removes extracted distributions when closed.

    Returns:
        The distribution directory, or the expected KGP download path when no home
        is configured. This function does not download the distribution itself.
    """
    host = {("Darwin", "arm64"): "macos-aarch64", ("Darwin", "x86_64"): "macos-x86_64", ("Linux", "x86_64"): "linux-x86_64"}.get((platform.system(), platform.machine()))
    if host is None:
        raise ValueError("Only macOS arm64/x64 and Linux x64 are supported")
    native_home = compiler_config.kotlin_native_home
    if native_home is None:
        # Match the tool-local KONAN_DATA_DIR passed to Gradle; KGP performs the download.
        base = PROJECT_ROOT / ".konan"
        return (base / f"kotlin-native-prebuilt-{host}-{compiler_config.kotlin_version}").resolve()
    if isinstance(native_home, list):
        if not native_home or any(not path.name.endswith((".tgz", ".tar.gz")) for path in native_home):
            raise ValueError("A kotlin_native_home list must contain only .tgz or .tar.gz archives")
        matches = [path for path in native_home if host in path.name]
        if len(matches) != 1:
            names = ", ".join(path.name for path in native_home)
            raise ValueError(f"Expected exactly one kotlin_native_home archive containing {host}; found {len(matches)} in [{names}]")
        native_home_path = matches[0]
    else:
        native_home_path = native_home
    if native_home_path.name.endswith((".tgz", ".tar.gz")):
        # extract from archive, every time
        extraction_dir = Path(extraction_cleanup.enter_context(tempfile.TemporaryDirectory(prefix="ic-fuzzer-", dir="/tmp")))
        archive_path = compiler_config_directory / native_home_path
        with tarfile.open(archive_path) as archive:
            archive.extractall(extraction_dir, filter="data")
        candidates = list(extraction_dir.glob("*/bin/konanc")) + list(extraction_dir.glob("bin/konanc"))
        if len(candidates) != 1:
            raise ValueError(f"The archive must contain exactly one bin/konanc: {native_home_path}")
        return candidates[0].parent.parent.resolve()
    if not native_home_path.is_absolute():
        # if it's a path, requires absolute path
        raise ValueError(f"Compiler directory must be an absolute path: {native_home_path}")
    if not (native_home_path / "bin/konanc").is_file():
        raise ValueError(f"Cannot find bin/konanc: {native_home_path}")
    return native_home_path.resolve()
