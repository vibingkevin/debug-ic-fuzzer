"""Reuse a bare repository and create two independent worktrees per run."""

from contextlib import contextmanager
from pathlib import Path
import hashlib
import subprocess

from impl.config import Project


def _run_git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "--git-dir", str(repository), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip())
    return result.stdout.strip()


def create_worktree(repository: Path, directory: Path, commit: str):
    _run_git(repository, "worktree", "add", "--detach", str(directory), commit)


@contextmanager
def checkout_worktree(repository: Path, variant_directory: Path, commit: str):
    build_directory = variant_directory.parent / "project"
    _run_git(repository, "worktree", "move", str(variant_directory), str(build_directory))
    try:
        subprocess.run(
            ["git", "-C", str(build_directory), "checkout", "--detach", commit],
            check=True, capture_output=True, text=True,
        )
        yield build_directory
    finally:
        _run_git(repository, "worktree", "move", str(build_directory), str(variant_directory))


def prepare_repository(root: Path, project: Project) -> tuple[Path, list[str]]:
    root.mkdir(parents=True, exist_ok=True)
    name = project.repository.rsplit("/", 1)[-1].removesuffix(".git")
    key = hashlib.md5(project.repository.encode()).hexdigest()[:8]
    repository = root / f"{name}-{key}.git"
    if not repository.exists():
        subprocess.run(
            ["git", "clone", "--bare", project.repository, str(repository)], check=True
        )
    # Update the remote ref without changing any worktree HEAD.
    ref = f"refs/remotes/origin/{project.branch}"
    _run_git(repository, "fetch", "origin", f"+refs/heads/{project.branch}:{ref}")
    history = _run_git(
        repository, "rev-list", "--first-parent", "--reverse", ref
    ).splitlines()
    start = _run_git(repository, "rev-parse", "--verify", project.start_commit + "^{commit}")
    end = _run_git(repository, "rev-parse", "--verify", project.end_commit + "^{commit}")
    if (
        start not in history
        or end not in history
        or history.index(start) > history.index(end)
    ):
        raise ValueError(
            "Start and end commits must be on the selected branch, with start no later than end"
        )
    commits = history[history.index(start):history.index(end) + 1]
    return repository, commits if project.direction == "forward" else commits[::-1]
