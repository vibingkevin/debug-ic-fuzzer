"""移动工程、执行一次 Gradle、结束后记录结果。"""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import time

from impl.config import Compiler, Project
from impl.repository import git


def init_script() -> Path:
    source = Path(__file__).parent.parent / 'capture.init.gradle'
    return source if source.exists() else Path(sys.prefix) / 'share/debug-ic-fuzzer/capture.init.gradle'


def build(repository: Path, destination: Path, name: str, revision: str,
          project: Project, compiler: Compiler, home: Path, config_dir: Path) -> dict:
    parked, active = destination / name, destination / 'project'
    git(repository, 'worktree', 'move', str(parked), str(active))
    command = [str(active / 'gradlew'), project.task,
               *[arg.replace('{config_dir}', str(config_dir)) for arg in project.gradle_args],
               '--console=plain', '--info', '-I', str(init_script()),
               f'-Pic.kotlin.version={compiler.kotlin_version}',
               '-Pkotlin.incremental.native=true', '-Pkotlin.internal.compiler.arguments.log.level=info']
    if compiler.kotlin_native_home is not None:
        command.append(f'-Pkotlin.native.home={home}')
    log = destination / f'.{name}.log'
    started = time.monotonic()
    try:
        # 不逐行处理；结束后解析少量任务信息，完整日志（含 konanc 参数）保留。
        with log.open('w') as output:
            code = subprocess.run(command, cwd=active, stdout=output, stderr=subprocess.STDOUT).returncode
    finally:
        git(repository, 'worktree', 'move', str(active), str(parked))
    seconds = time.monotonic() - started
    content = log.read_text(errors='replace').replace('\r\n', '\n').replace('\r', '\n').strip().encode()
    digest = hashlib.md5(content).hexdigest()
    stored = destination / 'store' / 'gradle.log' / f'gradle.log_{digest}'
    stored.parent.mkdir(parents=True, exist_ok=True)
    if not stored.exists():
        stored.write_bytes(content)
    log.unlink()
    calls = [json.loads(line.removeprefix('IC_FUZZER '))
             for line in content.decode().splitlines() if line.startswith('IC_FUZZER ')]
    error = None
    if code == 0:
        if len(calls) != 1:
            error = '没有实际执行唯一的 Native link（请检查目标是否支持当前宿主机）'
        elif Path(calls[0]['home']).resolve() != home:
            error = '实际 Native home 与配置不一致'
        elif not Path(calls[0]['output']).is_relative_to(active):
            error = '最终输出位于工程外'
        elif not (parked / Path(calls[0]['output']).relative_to(active)).exists():
            error = '最终输出不存在'
    return dict(commit=revision, variant=name, command=command, cwd=str(active),
                kotlin_version=compiler.kotlin_version, native_home=str(home),
                exit_code=code, error=error, seconds=seconds, link=calls[-1] if calls else None,
                log=str(stored.relative_to(destination)))
