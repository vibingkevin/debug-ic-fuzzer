import csv
import json
from pathlib import Path
import subprocess

from impl.summary import read_commit_change_stats, write_run_summary


def test_summary_commit_changes_and_missing_values(tmp_path):
    """Use Git and JSON fixtures under tmp_path to check root, merge, binary, rename, and skipped rows."""
    source = tmp_path / "source"
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)

    def source_git(*git_args):
        """Run git_args in the fixture repository with a local test identity; return stdout."""
        return subprocess.check_output(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.test", *git_args], text=True).strip()

    (source / "code.txt").write_text("one\ntwo\n")
    (source / "binary.dat").write_bytes(b"\x00binary")
    source_git("add", ".")
    source_git("commit", "-m", "initial")
    initial = source_git("rev-parse", "HEAD")
    source_git("mv", "code.txt", "renamed.txt")
    source_git("commit", "-m", "rename")
    renamed = source_git("rev-parse", "HEAD")
    source_git("checkout", "-b", "side")
    (source / "side.txt").write_text("side\n")
    source_git("add", ".")
    source_git("commit", "-m", "side")
    source_git("checkout", "main")
    source_git("merge", "--no-ff", "side", "-m", "merge")
    merged = source_git("rev-parse", "HEAD")
    repository = source / ".git"
    assert read_commit_change_stats(repository, initial) == dict(changed_files=2, added_lines=2, deleted_lines=0)
    assert read_commit_change_stats(repository, renamed) == dict(changed_files=2, added_lines=2, deleted_lines=2)
    assert read_commit_change_stats(repository, merged) == dict(changed_files=1, added_lines=1, deleted_lines=0)

    run_directory = tmp_path / "run"
    run_directory.mkdir()
    # Reversed order must not reverse the commit's own added/deleted line counts.
    for index, commit in enumerate((merged, renamed, initial)):
        stem = f"{index:04d}-{commit[:12]}"
        for variant in ("baseline", "sut"):
            record = dict(commit=commit, task_state="UP-TO-DATE" if index == 1 else "EXECUTED", seconds=1.25 if variant == "baseline" else 2.5, rebuilt_cache_count=3 if index == 0 else None)
            (run_directory / f"{stem}-{variant}.json").write_text(json.dumps(record))
        comparison = dict(strict_equal=False, production_equal=True) if index == 0 else dict(strict_equal=None, production_equal=None) if index == 1 else {"error": "Build failed"}
        (run_directory / f"{stem}-comparison.json").write_text(json.dumps(comparison))
    summary_path = write_run_summary(run_directory, repository)
    with summary_path.open(newline="") as summary:
        rows = list(csv.DictReader(summary))
    assert [row["commit"] for row in rows] == [merged, renamed, initial]
    assert (rows[0]["strict_equal"], rows[0]["production_equal"]) == ("False", "True")
    assert (rows[0]["baseline_seconds"], rows[0]["sut_seconds"]) == ("1.25", "2.5")
    assert rows[0]["baseline_rebuilt_caches"] == rows[0]["sut_rebuilt_caches"] == "3"
    assert rows[1]["baseline_rebuilt_caches"] == rows[1]["sut_rebuilt_caches"] == "0"
    assert rows[1]["strict_equal"] == rows[1]["production_equal"] == ""
    assert rows[2]["strict_equal"] == rows[2]["production_equal"] == rows[2]["baseline_rebuilt_caches"] == ""
    assert rows[1]["added_lines"] == rows[1]["deleted_lines"] == "2"
