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
        return "Expected one Native link"
    if Path(link["home"]).resolve() != Path(build_result["native_home"]):
        return "Actual Native home does not match the configuration"
    output = Path(link["output"])
    project_directory = Path(build_result["cwd"])
    if not output.is_relative_to(project_directory):
        return "Final output is outside the project"
    if not output.exists():
        return "Final output does not exist"
    return None


def validate_cache_paths(build_result: dict, auto_cache_directory: Path, explicit_cache_paths: list[str], built_cache_report: str | None) -> list[tuple[Path, str]]:
    """Validate cache boundaries and return only reported cache directories with recorded prefixes.

    Args:
        build_result: Build record with cwd, native_home, exit_code, and resolved link metadata.
        auto_cache_directory: This variant's dependency cache directory inside the run.
        explicit_cache_paths: Explicit cache paths parsed from this invocation's compiler options.
        built_cache_report: Current built-cache report, or None when no current report applies.

    The compiler's klib/cache, fixed auto-cache, and link's IC directory are allowed boundaries,
    not scan targets. Select each reported <cache-unit>/bin/*.a's cache-unit directory once.
    Raise ValueError for missing reports/archives, invalid archive layout, or escaped paths.
    """
    project_directory = Path(build_result["cwd"])
    roots = [(Path(build_result["native_home"]) / "klib/cache", "native-home/klib/cache")]
    if not is_within_directory(auto_cache_directory, project_directory.parent):
        raise ValueError("Auto cache is outside the run directory")
    roots.append((auto_cache_directory, "auto-cache"))
    link = build_result["link"]
    if link is not None:
        if "ic_cache_dir" in link:
            ic = Path(link["ic_cache_dir"])
            if not ic.is_relative_to(project_directory) or not is_within_directory(ic, project_directory):
                raise ValueError(f"IC cache is outside the project: {ic}")
            roots.append((ic, str(Path("project") / ic.relative_to(project_directory))))
        elif build_result["exit_code"] == 0:
            raise ValueError("Expected one -Xic-cache-dir for the final link")
    archive_paths = []
    if built_cache_report is not None:
        report = Path(built_cache_report)
        if not is_within_directory(report, project_directory):
            raise ValueError("Built-cache report is outside the project")
        if report.exists():
            archive_paths = [Path(path) for path in report.read_text().splitlines() if path]
        elif build_result["exit_code"] == 0:
            raise ValueError("The compiler did not write its built-cache report")
    for path in [*explicit_cache_paths, *archive_paths]:
        if not any(is_within_directory(Path(path), root) for root, _ in roots):
            raise ValueError(f"Cache path is outside tracked roots: {path}")
    cache_directories = {}
    for archive in archive_paths:
        if archive.parent.name != "bin" or archive.suffix != ".a" or not archive.is_file():
            raise ValueError(f"Expected a reported cache archive at <cache-unit>/bin/*.a: {archive}")
        cache_directory = archive.parent.parent
        for root, prefix in roots:
            if cache_directory.is_relative_to(root) and is_within_directory(cache_directory, root):
                cache_directories[cache_directory] = str(Path(prefix) / cache_directory.relative_to(root))
                break
        else:
            raise ValueError(f"Cache directory is outside tracked roots: {cache_directory}")
    return sorted(cache_directories.items())
