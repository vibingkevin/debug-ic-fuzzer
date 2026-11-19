"""Enforce the replay rules for the first migrated external project."""

import json
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_nytimes_project_rules():
    workflow_path = ".github/workflows/nytimes-kmp.yml"
    project_path = "configs/nytimes-kmp.yaml"
    workflow = yaml.safe_load((ROOT / workflow_path).read_text())
    project = yaml.safe_load((ROOT / project_path).read_text())
    # PyYAML's YAML 1.1 loader interprets the unquoted Actions key "on" as True.
    paths = workflow[True]["pull_request"]["paths"]
    for dependency in (workflow_path, project_path, "configs/compilers-2.4.21-9267.yaml"):
        assert dependency in paths
    for index, arg in enumerate(project["gradle_args"]):
        if arg == "-I":
            script = project["gradle_args"][index + 1].replace("{config_dir}/", "configs/")
            assert (ROOT / script).is_file()
            assert script in paths
    assert project["task"] == ":app:compose:linkDebugFrameworkIosArm64"
    assert project["end_commit"] == "latest"
    job = workflow["jobs"]["replay"]
    assert job["timeout-minutes"] <= 360
    step = next(step for step in job["steps"] if step.get("name") == "Replay page")
    assert "matrix.start_at" in step["env"]["START_AT"]
    assert "matrix.guard" in step["env"]["GUARD"]
    replay = next(step["run"] for step in job["steps"] if step.get("name") == "Replay page")
    page_size = int(re.search(r"--commit-count (\d+)", replay)[1])
    assert page_size > 0 and page_size % 5 == 0
    entries = job["strategy"]["matrix"]["include"]
    pages = [entry["start_at"] for entry in entries if not entry.get("guard")]
    assert pages == list(range(0, entries[-1]["start_at"], page_size))
    assert entries[-1].get("guard") is True
    sample = yaml.safe_load((ROOT / ".github/workflows/ic-sample.yml").read_text())
    assert 0 < job["strategy"]["max-parallel"] + sample["jobs"]["replay"]["strategy"]["max-parallel"] <= 5


@pytest.mark.parametrize(("exit_code", "error", "valid"), [(0, None, True), (1, None, False), (0, "collection failed", False), (None, None, False)])
def test_nytimes_gate_rejects_unsuccessful_builds(tmp_path, exit_code, error, valid):
    workflow = yaml.safe_load((ROOT / ".github/workflows/nytimes-kmp.yml").read_text())
    script = next(step["run"] for step in workflow["jobs"]["replay"]["steps"] if step.get("name") == "Require successful builds")
    script = script.replace(".venv/bin/python", sys.executable)
    run = tmp_path / "run/page"
    run.mkdir(parents=True)
    for variant in ("baseline", "sut"):
        (run / f"0000-abc-{variant}.json").write_text(json.dumps(dict(exit_code=exit_code, error=error)))
    result = subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path, capture_output=True, text=True)
    assert (result.returncode == 0) is valid, result.stderr
    if valid:
        for path in run.glob("*.json"):
            path.unlink()
        assert subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path, capture_output=True).returncode != 0
