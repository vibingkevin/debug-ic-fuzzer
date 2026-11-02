"""Move the project, run Gradle once, and record the result after completion."""

from pathlib import Path
import json
import re
import subprocess
import sys
import time

from impl.config import Compiler, Project
from impl.repository import checkout_worktree
from impl.validation import validate_result


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
    record_name: str,
    last_link: dict | None = None,
) -> dict:
    variant_directory = destination / name
    log = destination / f"{record_name}.gradle.log"
    started = time.monotonic()
    with checkout_worktree(repository, variant_directory, revision) as build_directory:
        command = [
            str(build_directory / "gradlew"),
            project.task,
            *[arg.replace("{config_dir}", str(config_dir)) for arg in project.gradle_args],
            "--gradle-user-home",
            str(Path.cwd() / ".gradle-home"),
            "--console=plain",
            "-I",
            str(init_script()),
            f"-Pic.kotlin.version={compiler.kotlin_version}",
            "-Pkotlin.incremental.native=true",
            "-Pkotlin.internal.compiler.arguments.log.level=warning",
        ]
        if compiler.kotlin_native_home is not None:
            command.append(f"-Pkotlin.native.home={home}")
        # Parse task details only after completion; keep the full log, including konanc arguments.
        with log.open("w") as output:
            code = subprocess.run(
                command, cwd=build_directory, stdout=output, stderr=subprocess.STDOUT
            ).returncode
    seconds = time.monotonic() - started
    log_text = log.read_text(errors="replace")
    task_states = re.findall(r"^> Task " + re.escape(project.task) + r"(?: ([A-Z-]+))?$", log_text, re.MULTILINE)
    task_state = (task_states[-1] or "EXECUTED") if task_states else None
    calls = [
        json.loads(line.removeprefix("IC_FUZZER "))
        for line in log_text.splitlines()
        if line.startswith("IC_FUZZER ")
    ]
    # Only carry task metadata for UP-TO-DATE; never compare with previous results.
    link = calls[0] if len(calls) == 1 else None
    if not calls and task_state == "UP-TO-DATE":
        link = last_link
    result = dict(
        commit=revision,
        variant=name,
        command=command,
        cwd=str(build_directory),
        kotlin_version=compiler.kotlin_version,
        native_home=str(home),
        exit_code=code,
        seconds=seconds,
        task_state=task_state,
        link=link,
        log=log.name,
    )
    result["error"] = validate_result(result, variant_directory)
    return result
