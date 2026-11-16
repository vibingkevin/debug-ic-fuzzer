"""Cover run selection and failures that finish before their matrix siblings."""

import json

import pytest

from scripts import watch_actions as monitor


def test_latest_runs_pages_past_busy_workflow_and_ignores_disabled(monkeypatch):
    monkeypatch.setattr(monitor, "pages", lambda *_: [dict(id=1, state="active"), dict(id=2, state="active"), dict(id=3, state="disabled_manually")])
    first = [dict(id=300 - i, workflow_id=1, name="A") for i in range(100)]
    second = [dict(id=200, workflow_id=3, name="Disabled"), dict(id=199, workflow_id=2, name="B")]
    queries = []

    def gh(*args):
        queries.append(args[-1])
        return json.dumps(dict(workflow_runs=second if "page=2" in args[-1] else first))

    monkeypatch.setattr(monitor, "gh", gh)
    assert [run["id"] for run in monitor.latest_runs("owner/repo", "main")] == [300, 199]
    assert len(queries) == 2
    assert all("branch=main" in query for query in queries)


def test_partial_matrix_failure_logs_retry_and_rerun(monkeypatch, tmp_path):
    run = dict(id=10, workflow_id=1, run_attempt=1, name="Replay", status="in_progress", conclusion=None, html_url="run-url")
    job = dict(id=20, name="page-50", status="completed", conclusion="failure", html_url="job-url", steps=[dict(name="Replay", conclusion="failure")])
    monkeypatch.setattr(monitor, "latest_runs", lambda *_: [run])
    monkeypatch.setattr(monitor, "pages", lambda *_: [job, dict(id=21, status="in_progress", conclusion=None)])
    calls = []

    def gh(*args):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("Log not ready")
        return "failure evidence\n"

    monkeypatch.setattr(monitor, "gh", gh)
    previous, reported = {}, set()
    assert monitor.poll("owner/repo", "main", tmp_path, previous, reported) == (False, True)
    assert not reported
    assert monitor.poll("owner/repo", "main", tmp_path, previous, reported) == (False, True)
    assert (tmp_path / "10-attempt-1-job-20.log").read_text() == "failure evidence\n"
    monitor.poll("owner/repo", "main", tmp_path, previous, reported)
    assert len(calls) == 2
    run["run_attempt"] = 2
    monitor.poll("owner/repo", "main", tmp_path, previous, reported)
    assert (tmp_path / "10-attempt-2-job-20.log").exists()
    assert len(calls) == 3
    assert json.loads((tmp_path / "latest.json").read_text())["runs"][0]["run_attempt"] == 2


def test_successful_rerun_clears_previous_failure(monkeypatch, tmp_path):
    run = dict(id=10, workflow_id=1, run_attempt=2, name="Replay", status="completed", conclusion="success", html_url="run-url")
    monkeypatch.setattr(monitor, "latest_runs", lambda *_: [run])
    monkeypatch.setattr(monitor, "pages", lambda *_: [dict(id=22, status="completed", conclusion="success"), dict(id=23, status="completed", conclusion="skipped")])
    assert monitor.poll("owner/repo", "main", tmp_path, {}, {(10, 1, 20)}) == (True, False)


@pytest.mark.parametrize("steps", [[], None])
def test_cancelled_job_without_steps_does_not_download_nonexistent_logs(monkeypatch, tmp_path, steps):
    run = dict(id=10, workflow_id=1, run_attempt=1, name="Replay", status="completed", conclusion="cancelled", html_url="run-url")
    job = dict(id=20, name="page-50", status="completed", conclusion="cancelled", html_url="job-url", steps=steps)
    monkeypatch.setattr(monitor, "latest_runs", lambda *_: [run])
    monkeypatch.setattr(monitor, "pages", lambda *_: [job])

    def unexpected_download(*args):
        pytest.fail("Cancelled jobs that never started do not have logs")

    monkeypatch.setattr(monitor, "gh", unexpected_download)
    reported = set()
    assert monitor.poll("owner/repo", "main", tmp_path, {}, reported) == (True, True)
    assert (10, 1, 20) in reported
    assert json.loads((tmp_path / "latest.json").read_text())["runs"][0]["jobs"][0]["conclusion"] == "cancelled"
