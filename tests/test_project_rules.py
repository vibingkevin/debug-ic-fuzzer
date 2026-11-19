"""Enforce the replay rules for the first migrated external project."""

from pathlib import Path

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
    assert "strategy" not in job
    replay = next(step["run"] for step in job["steps"] if step.get("name") == "Replay full range")
    assert "--project configs/nytimes-kmp.yaml" in replay
    assert "--start-at" not in replay
    assert "--commit-count" not in replay
