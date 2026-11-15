"""Summarize completed commit pairs without reading build outputs or changing verdicts."""

import csv
import json
from pathlib import Path

from impl.repository import run_repository_git


def read_commit_change_stats(bare_repository: Path, commit_id: str) -> dict:
    """Count changed files and text lines for commit_id in bare_repository.

    Compare against the first parent, or the empty tree for a root commit, regardless
    of replay direction. Count binary files but not their lines; treat renames as delete/add.
    """
    changes = run_repository_git(bare_repository, "show", "--format=", "--numstat", "--no-renames", "--diff-merges=first-parent", commit_id, "--").splitlines()
    stats = dict(changed_files=len(changes), added_lines=0, deleted_lines=0)
    for change in changes:
        added, deleted, _ = change.split("\t", 2)
        stats["added_lines"] += int(added) if added != "-" else 0
        stats["deleted_lines"] += int(deleted) if deleted != "-" else 0
    return stats


def write_run_summary(run_directory: Path, bare_repository: Path) -> Path:
    """Write summary.csv from completed JSON pairs in run_directory and Git commit stats.

    bare_repository supplies commit changes; JSON supplies comparisons, elapsed seconds,
    and reported archive counts. Preserve replay order and leave unknown values blank.
    Return the CSV path; do not inspect caches, outputs, or overwritten compiler reports.
    """
    columns = ["commit", "changed_files", "added_lines", "deleted_lines", "strict_equal", "production_equal", "baseline_seconds", "sut_seconds", "baseline_rebuilt_caches", "sut_rebuilt_caches"]
    summary_path = run_directory / "summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        for comparison_path in sorted(run_directory.glob("*-comparison.json")):
            record_stem = comparison_path.name.removesuffix("-comparison.json")
            comparison = json.loads(comparison_path.read_text())
            variants = {variant: json.loads((run_directory / f"{record_stem}-{variant}.json").read_text()) for variant in ("baseline", "sut")}
            commit_id = variants["baseline"]["commit"]
            row = dict(commit=commit_id, **read_commit_change_stats(bare_repository, commit_id), strict_equal=comparison.get("strict_equal"), production_equal=comparison.get("production_equal"))
            for variant, result in variants.items():
                row[f"{variant}_seconds"] = result.get("seconds")
                row[f"{variant}_rebuilt_caches"] = 0 if result["task_state"] == "UP-TO-DATE" else result.get("rebuilt_cache_count")
            writer.writerow(row)
    return summary_path
