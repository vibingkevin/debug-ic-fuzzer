"""Validate the completed build without changing its files."""

from pathlib import Path


def validate_result(result: dict) -> str | None:
    if result["exit_code"] != 0:
        return None
    if result["task_state"] == "FROM-CACHE":
        return "The final Native link must not be restored from Build Cache"
    link = result["link"]
    if link is None:
        return "Expected one Native link or UP-TO-DATE with a previously executed link"
    if Path(link["home"]).resolve() != Path(result["native_home"]):
        return "Actual Native home does not match the configuration"
    output = Path(link["output"])
    build_directory = Path(result["cwd"])
    if not output.is_relative_to(build_directory):
        return "Final output is outside the project"
    if not output.exists():
        return "Final output does not exist"
    return None
