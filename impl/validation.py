"""Validate the completed build without changing its files."""

from pathlib import Path

from impl.collection import is_within_directory


def validate_link_result(build_result: dict) -> str | None:
    """Check a successful build record's link state, Native home, and output location.

    build_result is the parsed build record. Return an error message or None; failed builds
    return None because their nonzero exit_code already signals failure to the caller.
    """
    if build_result["exit_code"] != 0:
        return None
    if build_result["task_state"] == "FROM-CACHE":
        return "The final Native link must not be restored from Build Cache"
    link = build_result["link"]
    if link is None:
        return "Expected one Native link or UP-TO-DATE with a previously executed link"
    if Path(link["home"]).resolve() != Path(build_result["native_home"]):
        return "Actual Native home does not match the configuration"
    output = Path(link["output"])
    project_directory = Path(build_result["cwd"])
    if not output.is_relative_to(project_directory):
        return "Final output is outside the project"
    if not output.exists():
        return "Final output does not exist"
    return None


def validate_cache_paths(build_result: dict, explicit_cache_paths: list[str], built_cache_report: str | None) -> list[tuple[Path, str]]:
    """Validate cache boundaries and return (physical root, logical prefix) pairs.

    Args:
        build_result: Build record with cwd, native_home, exit_code, and resolved link metadata.
        explicit_cache_paths: Explicit cache paths parsed from this invocation's compiler options.
        built_cache_report: Current built-cache report, or None when no current report applies.

    Track the compiler's klib/cache and the link's project-local IC directory. Raise
    ValueError for missing required metadata/reports or paths outside tracked boundaries.
    """
    project_directory = Path(build_result["cwd"])
    roots = [(Path(build_result["native_home"]) / "klib/cache", "native-home/klib/cache")]
    link = build_result["link"]
    if link is not None:
        if "ic_cache_dir" in link:
            ic = Path(link["ic_cache_dir"])
            if not ic.is_relative_to(project_directory) or not is_within_directory(ic, project_directory):
                raise ValueError(f"IC cache is outside the project: {ic}")
            roots.append((ic, str(Path("project") / ic.relative_to(project_directory))))
        elif build_result["exit_code"] == 0:
            raise ValueError("Expected one -Xic-cache-dir for the final link")
    paths = list(explicit_cache_paths)
    if built_cache_report is not None:
        report = Path(built_cache_report)
        if not is_within_directory(report, project_directory):
            raise ValueError("Built-cache report is outside the project")
        if report.exists():
            paths.extend(path for path in report.read_text().splitlines() if path)
        elif build_result["exit_code"] == 0:
            raise ValueError("The compiler did not write its built-cache report")
    for path in paths:
        if not any(is_within_directory(Path(path), root) for root, _ in roots):
            raise ValueError(f"Cache path is outside tracked roots: {path}")
    return roots
