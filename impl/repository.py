"""Reuse a bare repository and create two independent worktrees per run."""

from contextlib import contextmanager
from pathlib import Path
import subprocess

from impl.config import Project


def _run_git(repository: Path, *args: str) -> str:
    """Run Git with repository as its Git directory and args as command arguments.

    Return stripped stdout; raise ValueError with stderr if Git fails.
    """
    result = subprocess.run(["git", "--git-dir", str(repository), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise ValueError(result.stderr.strip())
    return result.stdout.strip()


def create_worktree(repository: Path, directory: Path, commit: str):
    """Create a detached worktree at directory for commit in the bare repository."""
    _run_git(repository, "worktree", "add", "--detach", str(directory), commit)


@contextmanager
def checkout_worktree(repository: Path, variant_directory: Path, commit: str):
    """Activate a variant at the stable project path and restore it on context exit.

    Args:
        repository: Bare repository that owns the worktree.
        variant_directory: Parked baseline or sut worktree to move beside itself as project.
        commit: Revision to check out with detached HEAD before yielding.

    Yields:
        The active project directory. Restores its original location even if checkout
        or the context body fails, retaining untracked build outputs.
    """
    build_directory = variant_directory.parent / "project"
    _run_git(repository, "worktree", "move", str(variant_directory), str(build_directory))
    try:
        subprocess.run(["git", "-C", str(build_directory), "checkout", "--detach", commit], check=True, capture_output=True, text=True)
        yield build_directory
    finally:
        _run_git(repository, "worktree", "move", str(build_directory), str(variant_directory))


def prepare_repository(root: Path, project: Project) -> tuple[Path, list[str]]:
    """Fetch the selected source and return its bare repository and ordered commit IDs.

    Args:
        root: Shared repository directory; repositories with the same name share a clone.
        project: Repository URL, branch, inclusive commit endpoints, and replay direction.

    Different URLs use separate remotes. Reject endpoints outside the selected branch's
    main line or in the wrong order before applying the requested replay direction.
    """
    root.mkdir(parents=True, exist_ok=True)
    name = project.repository.rsplit("/", 1)[-1].removesuffix(".git")
    repository = root / f"{name}.git"
    if not repository.exists():
        subprocess.run(["git", "clone", "--bare", project.repository, str(repository)], check=True)
    remotes = _run_git(repository, "remote").splitlines()
    for remote in remotes:
        if _run_git(repository, "remote", "get-url", remote) == project.repository:
            break
    else:
        remote = f"remote-{len(remotes)}"
        _run_git(repository, "remote", "add", remote, project.repository)
    # Update the remote ref without changing any worktree HEAD.
    ref = f"refs/remotes/{remote}/{project.branch}"
    _run_git(repository, "fetch", remote, f"+refs/heads/{project.branch}:{ref}")
    history = _run_git(repository, "rev-list", "--first-parent", "--reverse", ref).splitlines()
    start = _run_git(repository, "rev-parse", "--verify", project.start_commit + "^{commit}")
    end = _run_git(repository, "rev-parse", "--verify", project.end_commit + "^{commit}")
    if start not in history or end not in history or history.index(start) > history.index(end):
        raise ValueError("Start and end commits must be on the selected branch, with start no later than end")
    commits = history[history.index(start) : history.index(end) + 1]
    return repository, commits if project.direction == "forward" else commits[::-1]
