"""Check rotation order, API behavior, and the README/workflow contract."""

from io import BytesIO
from pathlib import Path
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from impl import rotation

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "vibingkevin/debug-ic-fuzzer"


def badge(workflow, repository=REPOSITORY):
    return f"[![Project](https://github.com/{repository}/actions/workflows/{workflow}/badge.svg?branch=main)](https://github.com/{repository}/actions/workflows/{workflow})"


def run(workflow, title="rotation / Project", status="completed", conclusion="success"):
    return dict(path=f".github/workflows/{workflow}", display_title=title, status=status, conclusion=conclusion, html_url="https://github.com/example/run")


def test_projects_follow_badge_order_and_ignore_other_repositories():
    readme = "\n".join([badge("b.yml"), badge("a.yml"), badge("b.yml"), badge("foreign.yml", "other/repo")])
    assert rotation.projects(readme, REPOSITORY) == ["b.yml", "a.yml"]
    with pytest.raises(ValueError, match="no project workflow badges"):
        rotation.projects("", REPOSITORY)


@pytest.mark.parametrize(
    "history,expected",
    [([], "a.yml"), ([run("a.yml")], "b.yml"), ([run("b.yml", conclusion="failure")], "c.yml"), ([run("c.yml")], "a.yml"), ([run("removed.yml")], "a.yml"), ([run("c.yml", "workflow_dispatch / Manual"), run("a.yml")], "b.yml")],
)
def test_next_project(history, expected):
    assert rotation.next_project(["a.yml", "b.yml", "c.yml"], iter(history)) == expected


def test_appending_project_continues_after_previous_last_project():
    assert rotation.next_project(["a.yml", "b.yml", "c.yml", "new.yml"], [run("c.yml")]) == "new.yml"
    assert rotation.next_project(["a.yml", "b.yml", "c.yml", "new.yml"], [run("new.yml")]) == "a.yml"


def test_workflow_runs_paginates(monkeypatch):
    calls = []

    def api(endpoint):
        query = parse_qs(urlsplit(endpoint).query)
        calls.append(query)
        return {"workflow_runs": [run("manual.yml", "Manual")] * 100 if query["page"] == ["1"] else [run("a.yml")]}

    monkeypatch.setattr(rotation, "api", api)
    assert rotation.next_project(["a.yml", "b.yml"], rotation.workflow_runs(event="workflow_dispatch", branch="main")) == "b.yml"
    assert [query["page"] for query in calls] == [["1"], ["2"]]
    assert all(query["event"] == ["workflow_dispatch"] and query["branch"] == ["main"] for query in calls)


@pytest.mark.parametrize("busy", [None, "queued", "in_progress", "pending", "waiting", "requested"])
def test_rotate_dispatches_once_or_skips_busy_repository(tmp_path, monkeypatch, busy):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    (tmp_path / "README.md").write_text(badge("a.yml") + "\n" + badge("b.yml"))
    monkeypatch.setattr(rotation, "scheduled_projects", lambda workflows: workflows)
    posted = []

    def api(endpoint, data=None):
        if data is not None:
            posted.append((endpoint, data))
            return
        query = parse_qs(urlsplit(endpoint).query)
        if "status" in query:
            status = query["status"][0]
            return {"workflow_runs": [run("a.yml", status=status)] if status == busy else [run("unrelated.yml", status=status)]}
        assert query["event"] == ["workflow_dispatch"] and query["branch"] == ["main"]
        return {"workflow_runs": [run("b.yml", "workflow_dispatch / Manual"), run("a.yml")]}

    monkeypatch.setattr(rotation, "api", api)
    rotation.rotate()
    assert posted == ([] if busy else [("actions/workflows/b.yml/dispatches", {"ref": "main", "inputs": {"scheduled": True}})])
    posted.clear()
    rotation.rotate(dry_run=True)
    assert not posted


def test_api_authenticates_and_posts_boolean_input(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com")

    def urlopen(request, timeout):
        assert request.full_url == f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/a.yml/dispatches"
        assert request.get_method() == "POST"
        assert request.get_header("Authorization") == "Bearer test-token"
        assert json.loads(request.data) == {"ref": "main", "inputs": {"scheduled": True}}
        assert timeout == 30
        return BytesIO(b"")

    monkeypatch.setattr(rotation, "urlopen", urlopen)
    assert rotation.api("actions/workflows/a.yml/dispatches", {"ref": "main", "inputs": {"scheduled": True}}) is None


def test_scheduled_projects_skip_workflows_without_opt_in(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / ".github/workflows"
    directory.mkdir(parents=True)
    (directory / "ready.yml").write_text("on:\n  workflow_dispatch:\n    inputs:\n      scheduled:\n        type: boolean\n")
    (directory / "later.yml").write_text("on:\n  workflow_dispatch:\n")
    assert rotation.scheduled_projects(["later.yml", "ready.yml"]) == ["ready.yml"]


def test_rotation_without_enabled_projects_does_not_dispatch(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    (tmp_path / "README.md").write_text(badge("a.yml"))
    monkeypatch.setattr(rotation, "workflow_runs", lambda **filters: iter([]))
    monkeypatch.setattr(rotation, "scheduled_projects", lambda workflows: [])
    monkeypatch.setattr(rotation, "api", lambda *args: pytest.fail("must not dispatch"))
    rotation.rotate()
    assert "no project has enabled" in capsys.readouterr().out


def test_registered_workflows_support_rotation(monkeypatch):
    monkeypatch.chdir(ROOT)
    readme = (ROOT / "README.md").read_text()
    workflows = rotation.projects(readme, REPOSITORY)
    assert set(workflows) == {path.name for path in (ROOT / ".github/workflows").glob("*.yml") if path.name != "replay-rotation.yml"}
    enabled = rotation.scheduled_projects(workflows)
    assert enabled
    for workflow in enabled:
        config = yaml.load((ROOT / ".github/workflows" / workflow).read_text(), Loader=yaml.BaseLoader)
        scheduled = config["on"]["workflow_dispatch"]["inputs"]["scheduled"]
        assert scheduled["type"] == "boolean" and scheduled["default"] == "false"
        assert "inputs.scheduled" in config["run-name"] and "'rotation'" in config["run-name"]
        assert "schedule" not in config["on"]
    scheduler = yaml.load((ROOT / ".github/workflows/replay-rotation.yml").read_text(), Loader=yaml.BaseLoader)
    assert scheduler["on"]["schedule"] == [{"cron": "17 0,12 * * *"}]
    assert scheduler["concurrency"]["cancel-in-progress"] == "false"
