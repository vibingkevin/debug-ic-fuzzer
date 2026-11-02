"""Reuse a bare repository and create two independent worktrees per run."""

from pathlib import Path
import hashlib
import subprocess

from impl.config import Project


def git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "--git-dir", str(repository), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip())
    return result.stdout.strip()


def prepare_repository(root: Path, project: Project) -> tuple[Path, str]:
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
    git(repository, "fetch", "origin", f"+refs/heads/{project.branch}:{ref}")
    history = git(
        repository, "rev-list", "--first-parent", "--reverse", ref
    ).splitlines()
    start = git(repository, "rev-parse", "--verify", project.start_commit + "^{commit}")
    end = git(repository, "rev-parse", "--verify", project.end_commit + "^{commit}")
    if (
        start not in history
        or end not in history
        or history.index(start) > history.index(end)
    ):
        raise ValueError(
            "Start and end commits must be on the selected branch, with start no later than end"
        )
    return repository, start if project.direction == "forward" else end
