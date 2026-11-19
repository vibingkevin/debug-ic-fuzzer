"""Dispatch the next README project using marked workflow runs as the cursor."""

from pathlib import Path
import json
import os
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def projects(readme: str, repository: str) -> list[str]:
    """Read this repository's workflow badges in their displayed order."""
    pattern = rf"https://github\.com/{re.escape(repository)}/actions/workflows/([\w-]+\.ya?ml)/badge\.svg"
    workflows = list(dict.fromkeys(re.findall(pattern, readme)))
    if not workflows:
        raise ValueError("README has no project workflow badges")
    return workflows


def next_project(workflows: list[str], runs) -> str:
    """Advance after the newest marked dispatch, including failed runs."""
    for run in runs:
        if run["display_title"].startswith("rotation / "):
            previous = Path(run["path"]).name
            return workflows[(workflows.index(previous) + 1) % len(workflows)] if previous in workflows else workflows[0]
    return workflows[0]


def api(endpoint: str, data=None):
    repository = os.environ["GITHUB_REPOSITORY"]
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    request = Request(
        f"{base}/repos/{repository}/{endpoint}",
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body else None


def workflow_runs(**filters):
    page = 1
    while True:
        runs = api("actions/runs?" + urlencode(dict(filters, per_page=100, page=page)))["workflow_runs"]
        yield from runs
        if len(runs) < 100:
            return
        page += 1


def rotate(*, dry_run=False):
    workflows = projects(Path("README.md").read_text(), os.environ["GITHUB_REPOSITORY"])
    paths = {f".github/workflows/{workflow}" for workflow in workflows}
    for status in ("queued", "in_progress", "pending", "waiting", "requested"):
        for run in workflow_runs(status=status):
            if run["path"] in paths:
                print(f"Skipping rotation: {run['html_url']} is {run['status']}.")
                return
    workflow = next_project(workflows, workflow_runs(event="workflow_dispatch", branch="main"))
    if dry_run:
        print(f"Would dispatch {workflow} on main.")
        return
    api(f"actions/workflows/{workflow}/dispatches", {"ref": "main", "inputs": {"scheduled": True}})
    print(f"Dispatched {workflow} on main.")


if __name__ == "__main__":
    rotate()
