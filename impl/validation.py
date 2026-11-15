"""Validate the completed build without changing its files."""

from pathlib import Path


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
