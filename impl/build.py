"""Run Gradle in the active project and parse its completed log."""

from pathlib import Path
import json
import re
import subprocess
import sys
import time

from impl.config import Compiler, Project
from impl.validation import validate_caches, validate_result


def init_script() -> Path:
    """Return the Gradle init script path from the source tree or installed package data."""
    script_path = Path(__file__).parent.parent / "capture.init.gradle"
    return script_path if script_path.exists() else Path(sys.prefix) / "share/debug-ic-fuzzer/capture.init.gradle"


def build(build_directory: Path, destination: Path, name: str, revision: str, project: Project, compiler: Compiler, home: Path, config_dir: Path, record_name: str, last_link: dict | None = None) -> tuple[dict, list[tuple[Path, str]]]:
    """Run one Gradle build, then parse and validate its completed log.

    Args:
        build_directory: Active project worktree containing gradlew.
        destination: Run directory where the Gradle log is written.
        name: Compiler variant, baseline or sut, recorded in the result.
        revision: Checked-out commit ID recorded in the result.
        project: Gradle task and additional arguments to execute.
        compiler: KGP version and whether an explicit Native home was configured.
        home: Resolved Native home to verify and, when configured, pass to Gradle.
        config_dir: Project YAML directory substituted for {config_dir} in Gradle arguments.
        record_name: Log filename stem, including commit index, ID, and variant.
        last_link: Previous link metadata reused only when this task is UP-TO-DATE.

    Returns:
        The build record and cache roots as (physical directory, logical prefix) pairs.
        Build exit status and validation errors are recorded; process launch errors propagate.
    """
    log = destination / f"{record_name}.gradle.log"
    started = time.monotonic()
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
        code = subprocess.run(command, cwd=build_directory, stdout=output, stderr=subprocess.STDOUT).returncode
    seconds = time.monotonic() - started
    log_text = log.read_text(errors="replace")
    task_states = re.findall(r"^> Task " + re.escape(project.task) + r"(?: ([A-Z-]+))?$", log_text, re.MULTILINE)
    task_state = (task_states[-1] or "EXECUTED") if task_states else None
    calls = [json.loads(line.removeprefix("IC_FUZZER ")) for line in log_text.splitlines() if line.startswith("IC_FUZZER ")]
    # Only carry task metadata for UP-TO-DATE; never compare with previous results.
    link = calls[0] if len(calls) == 1 else None
    if not calls and task_state == "UP-TO-DATE":
        link = last_link
    options = [line.strip() for line in log_text.splitlines()]
    cache_paths = []
    for arg in options:
        if arg.startswith(("-Xauto-cache-dir=", "-Xcache-directory=")):
            cache_paths.append(arg.split("=", 1)[1])
        elif arg.startswith("-Xcached-library="):
            cache_paths.append(arg.split("=", 1)[1].split(",", 1)[1])
    report_path = None
    if link is not None and task_state != "UP-TO-DATE":
        directories = {arg.split("=", 1)[1] for arg in options if arg.startswith("-Xic-cache-dir=")}
        if len(directories) == 1:
            link["ic_cache_dir"] = directories.pop()
        report_path = link.get("built_caches")
    result = dict(commit=revision, variant=name, command=command, cwd=str(build_directory), kotlin_version=compiler.kotlin_version, native_home=str(home), exit_code=code, seconds=seconds, task_state=task_state, link=link, log=log.name)
    result["error"] = validate_result(result)
    roots = [(home / "klib/cache", "native-home/klib/cache")]
    try:
        roots = validate_caches(result, cache_paths, report_path)
    except (ValueError, OSError) as error:
        result["error"] = result["error"] or str(error)
    return result, roots
