"""Enforce the replay rules for the first migrated external project."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_nytimes_project_rules():
    workflow_path = ".github/workflows/nytimes-kmp.yml"
    workflow = yaml.safe_load((ROOT / workflow_path).read_text())
    # PyYAML's YAML 1.1 loader interprets the unquoted Actions key "on" as True.
    paths = workflow[True]["pull_request"]["paths"]
    assert workflow_path in paths
    project_paths = {
        "configs/nytimes-kmp.yaml": (":app:compose:linkDebugFrameworkIosArm64", "latest"),
        "configs/nytimes-kmp-legacy.yaml": (":app:linkDebugFrameworkIosArm64", "6cf7d406500a91ee3e195faf78f3383230e628e1"),
    }
    for project_path, (task, end_commit) in project_paths.items():
        assert project_path in paths
        project = yaml.safe_load((ROOT / project_path).read_text())
        for index, arg in enumerate(project["gradle_args"]):
            if arg == "-I":
                script = project["gradle_args"][index + 1].replace("{config_dir}/", "configs/")
                assert (ROOT / script).is_file()
                assert script in paths
        assert project["task"] == task
        assert project["end_commit"] == end_commit
    assert "configs/compilers-2.4.21-9267.yaml" in paths
    job = workflow["jobs"]["replay"]
    matrix = job["strategy"]["matrix"]["include"]
    assert matrix == [
        {"range": "legacy", "project": "configs/nytimes-kmp-legacy.yaml"},
        {"range": "current", "project": "configs/nytimes-kmp.yaml"},
    ]
    replay = next(step["run"] for step in job["steps"] if step.get("name") == "Replay full range")
    assert '"$PROJECT"' in replay
    assert "--start-at" not in replay
    assert "--commit-count" not in replay


def test_feed_flow_project_rules():
    workflow_path = ".github/workflows/feed-flow.yml"
    project_path = "configs/feed-flow.yaml"
    workflow = yaml.safe_load((ROOT / workflow_path).read_text())
    project = yaml.safe_load((ROOT / project_path).read_text())
    paths = workflow[True]["pull_request"]["paths"]
    for dependency in (workflow_path, project_path, "configs/compilers-2.4.21-9267.yaml"):
        assert dependency in paths
    for index, arg in enumerate(project["gradle_args"]):
        if arg == "-I":
            script = project["gradle_args"][index + 1].replace("{config_dir}/", "configs/")
            assert (ROOT / script).is_file()
            assert script in paths
    assert project["task"] == ":shared:linkDebugFrameworkIosArm64"
    assert project["end_commit"] == "latest"
    job = workflow["jobs"]["replay"]
    matrix = job["strategy"]["matrix"]["include"]
    start_at = [page["start_at"] for page in matrix]
    assert start_at == list(range(0, 900, 50)) + [935]
    assert [page for page in matrix if page.get("guard")] == [{"start_at": 935, "guard": True}]
    replay = next(step["run"] for step in job["steps"] if step.get("name") == "Replay page")
    assert "--commit-count 50" in replay
