"""Move the project, run Gradle once, and record the result after completion."""

from pathlib import Path
import json
import subprocess
import sys
import time

from impl.config import Compiler, Project
from impl.repository import checkout_worktree


def init_script() -> Path:
    script_path = Path(__file__).parent.parent / "capture.init.gradle"
    return (
        script_path
        if script_path.exists()
        else Path(sys.prefix) / "share/debug-ic-fuzzer/capture.init.gradle"
    )


def build(
    repository: Path,
    destination: Path,
    name: str,
    revision: str,
    project: Project,
    compiler: Compiler,
    home: Path,
    config_dir: Path,
) -> dict:
    variant_directory = destination / name
    log = destination / f"{name}.gradle.log"
    started = time.monotonic()
    with checkout_worktree(repository, variant_directory, revision) as build_directory:
        command = [
            str(build_directory / "gradlew"),
            project.task,
            *[arg.replace("{config_dir}", str(config_dir)) for arg in project.gradle_args],
            "--console=plain",
            "--info",
            "-I",
            str(init_script()),
            f"-Pic.kotlin.version={compiler.kotlin_version}",
            "-Pkotlin.incremental.native=true",
            "-Pkotlin.internal.compiler.arguments.log.level=info",
        ]
        if compiler.kotlin_native_home is not None:
            command.append(f"-Pkotlin.native.home={home}")
        # Parse task details only after completion; keep the full log, including konanc arguments.
        with log.open("w") as output:
            code = subprocess.run(
                command, cwd=build_directory, stdout=output, stderr=subprocess.STDOUT
            ).returncode
    seconds = time.monotonic() - started
    calls = [
        json.loads(line.removeprefix("IC_FUZZER "))
        for line in log.read_text(errors="replace").splitlines()
        if line.startswith("IC_FUZZER ")
    ]
    error = None
    if code == 0:
        if len(calls) != 1:
            error = "Expected one executed Native link; check whether the target supports this host"
        elif Path(calls[0]["home"]).resolve() != home:
            error = "Actual Native home does not match the configuration"
        elif not Path(calls[0]["output"]).is_relative_to(build_directory):
            error = "Final output is outside the project"
        elif not (variant_directory / Path(calls[0]["output"]).relative_to(build_directory)).exists():
            error = "Final output does not exist"
    return dict(
        commit=revision,
        variant=name,
        command=command,
        cwd=str(build_directory),
        kotlin_version=compiler.kotlin_version,
        native_home=str(home),
        exit_code=code,
        error=error,
        seconds=seconds,
        link=calls[-1] if calls else None,
        log=log.name,
    )
