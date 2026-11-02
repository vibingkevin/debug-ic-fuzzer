"""Run Gradle in the active project and parse its completed log."""

from pathlib import Path
import json
import os
import re
import subprocess
import sys
import time

from impl import config
from impl.config import Compiler, Project
from impl.validation import validate_link_result


def gradle_init_script_path() -> Path:
    """Return the Gradle init script path from the source tree or installed package data."""
    script_path = config.PROJECT_ROOT / "capture.init.gradle"
    return script_path if script_path.exists() else Path(sys.prefix) / "share/debug-ic-fuzzer/capture.init.gradle"


def build_project(project_directory: Path, project_config: Project, compiler_config: Compiler, native_home: Path, auto_cache_directory: Path, record_stem: str) -> dict:
    """Run one Gradle build, then parse and validate its completed log.

    Args:
        project_directory: Active project worktree; its parent is the run directory.
        project_config: Gradle task and arguments, with config placeholders already expanded.
        compiler_config: KGP version and whether an explicit Native home was configured.
        native_home: Resolved Native distribution to verify and, when configured, pass to Gradle.
        auto_cache_directory: Variant-specific dependency cache beside the parked worktrees.
        record_stem: Filename stem for the Gradle log written beside the project directory.

    Returns:
        The build record. The caller
        adds commit and variant metadata. Exit status and validation errors are recorded;
        process launch errors propagate. UP-TO-DATE returns no link metadata.
    """
    auto_cache_directory.mkdir(parents=True, exist_ok=True)
    konan_data_directory = config.PROJECT_ROOT / ".konan"
    konan_data_directory.mkdir(exist_ok=True)
    command = [
        str(project_directory / "gradlew"),
        project_config.task,
        *project_config.gradle_args,
        "--gradle-user-home",
        str(config.PROJECT_ROOT / ".gradle-home"),
        "--console=plain",
        "-I",
        str(gradle_init_script_path()),
        f"-Pic.kotlin.version={compiler_config.kotlin_version}",
        f"-Pic.auto.cache.dir={auto_cache_directory}",
        "-Pkotlin.incremental.native=true",
        "-Pkotlin.internal.compiler.arguments.log.level=warning",
    ]
    if compiler_config.kotlin_native_home is not None:
        command.append(f"-Pkotlin.native.home={native_home}")

    gradle_log_path = project_directory.parent / f"{record_stem}.gradle.log"
    started = time.monotonic()
    with gradle_log_path.open("w") as output:
        code = subprocess.run(command, cwd=project_directory, env={**os.environ, "KONAN_DATA_DIR": str(konan_data_directory), "ZERO_AR_DATE": "1"}, stdout=output, stderr=subprocess.STDOUT).returncode
    seconds = time.monotonic() - started
    log_text = gradle_log_path.read_text(errors="replace")
    task_states = re.findall(r"^> Task " + re.escape(project_config.task) + r"(?: ([A-Z-]+))?$", log_text, re.MULTILINE)
    # Executed tasks have no status suffix; no matching task line means unknown state.
    task_state = (task_states[-1] or "EXECUTED") if task_states else None
    result = dict(
        command=command, cwd=str(project_directory), kotlin_version=compiler_config.kotlin_version, native_home=str(native_home), exit_code=code, seconds=seconds, task_state=task_state, link=None, log=gradle_log_path.name, error=None
    )
    if task_state == "UP-TO-DATE":
        return result
    native_link_reports = [json.loads(line.removeprefix("IC_FUZZER ")) for line in log_text.splitlines() if line.startswith("IC_FUZZER ")]
    result["link"] = native_link_reports[0] if len(native_link_reports) == 1 else None
    result["error"] = validate_link_result(result)
    return result
