"""读取配置，解析两版 Native home。"""
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
    model_config = ConfigDict(extra='forbid')


class Compiler(Config):
    kotlin_version: str
    kotlin_native_home: Path | None = None


class Compilers(Config):
    compilers: dict[Literal['baseline', 'sut'], Compiler]

    @model_validator(mode='after')
    def pair(self):
        if set(self.compilers) != {'baseline', 'sut'}:
            raise ValueError('compilers 必须同时提供 baseline 和 sut')
        return self


class Project(Config):
    repository: str = Field(pattern=r'^(?:https?://|ssh://|git@[^:]+:)')
    branch: str
    start_commit: str
    end_commit: str
    direction: Literal['forward', 'reverse'] = 'forward'
    task: str = Field(pattern=r'^:[^\s:]+(?::[^\s:]+)*$')
    gradle_args: list[str] = Field(default_factory=list)


def load(path: Path, model):
    return model.model_validate(yaml.safe_load(path.read_text()))


def prepare_home(compiler: Compiler, config_dir: Path, temporary: ExitStack) -> Path:
    host = {('Darwin', 'arm64'): 'macos-aarch64', ('Linux', 'x86_64'): 'linux-x86_64'}.get(
        (platform.system(), platform.machine()))
    if host is None:
        raise ValueError('仅支持 Apple Silicon Mac 和 Linux x64')
    source = compiler.kotlin_native_home
    if source is None:
        # 只推导路径以检查两版是否共用；下载交给 KGP，不传 native.home。
        base = Path(os.environ.get('KONAN_DATA_DIR', str(Path.home() / '.konan')))
        return (base / f'kotlin-native-prebuilt-{host}-{compiler.kotlin_version}').resolve()
    if source.name.endswith(('.tgz', '.tar.gz')):
        folder = Path(temporary.enter_context(tempfile.TemporaryDirectory(prefix='ic-fuzzer-', dir='/tmp')))
        with tarfile.open(config_dir / source) as archive:
            archive.extractall(folder, filter='data')
        candidates = list(folder.glob('*/bin/konanc')) + list(folder.glob('bin/konanc'))
        if len(candidates) != 1:
            raise ValueError(f'发行包中必须有唯一 bin/konanc：{source}')
        return candidates[0].parent.parent.resolve()
    if not source.is_absolute():
        raise ValueError(f'编译器目录必须是绝对路径：{source}')
    if not (source / 'bin/konanc').is_file():
        raise ValueError(f'找不到 bin/konanc：{source}')
    return source.resolve()
