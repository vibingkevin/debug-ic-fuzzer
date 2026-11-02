"""Reuse a bare repository and create two independent worktrees per run."""

from contextlib import contextmanager
from pathlib import Path
import subprocess

from impl.config import Project


def run_repository_git(bare_repository: Path, *git_args: str) -> str:
    """Run Git with bare_repository as its Git directory and git_args as command arguments.

    Return stripped stdout; raise ValueError with stderr if Git fails.
    """
    result = subprocess.run(["git", "--git-dir", str(bare_repository), *git_args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise ValueError(result.stderr.strip())
    return result.stdout.strip()


def create_detached_worktree(bare_repository: Path, worktree_directory: Path, commit_id: str):
    """Create a detached worktree at worktree_directory for commit_id in the bare repository."""
    run_repository_git(bare_repository, "worktree", "add", "--detach", str(worktree_directory), commit_id)


@contextmanager
def activate_worktree(bare_repository: Path, parked_worktree: Path, commit_id: str):
    """Activate a variant at the stable project path and restore it on context exit.

    Args:
        bare_repository: Bare repository that owns the worktree.
        parked_worktree: Parked baseline or sut worktree to move beside itself as project.
        commit_id: Revision to check out with detached HEAD before yielding.

    Yields:
        The active project directory. Restores its original location even if checkout
        or the context body fails, retaining untracked build outputs.
    """
    project_directory = parked_worktree.parent / "project"
    run_repository_git(bare_repository, "worktree", "move", str(parked_worktree), str(project_directory))
    try:
        subprocess.run(["git", "-C", str(project_directory), "checkout", "--detach", commit_id], check=True, capture_output=True, text=True)
        yield project_directory
    finally:
        run_repository_git(bare_repository, "worktree", "move", str(project_directory), str(parked_worktree))


def prepare_repository(repositories_directory: Path, project_config: Project) -> tuple[Path, list[str]]:
    """Fetch the selected source and return its bare repository and ordered commit IDs.

    Args:
        repositories_directory: Shared clone directory; repositories with the same name share a clone.
        project_config: Repository URL, branch, inclusive commit endpoints, and replay direction.

    Different URLs use separate remotes. Reject endpoints outside the selected branch's
    main line or in the wrong order before applying the requested replay direction.
    """
    repositories_directory.mkdir(parents=True, exist_ok=True)
    name = project_config.repository.rsplit("/", 1)[-1].removesuffix(".git")
    bare_repository = repositories_directory / f"{name}.git"
    if not bare_repository.exists():
        subprocess.run(["git", "clone", "--bare", project_config.repository, str(bare_repository)], check=True)
    remotes = run_repository_git(bare_repository, "remote").splitlines()
    for remote in remotes:
        if run_repository_git(bare_repository, "remote", "get-url", remote) == project_config.repository:
            break
    else:
        remote = f"remote-{len(remotes)}"
        run_repository_git(bare_repository, "remote", "add", remote, project_config.repository)
    # Update the remote ref without changing any worktree HEAD.
    ref = f"refs/remotes/{remote}/{project_config.branch}"
    run_repository_git(bare_repository, "fetch", remote, f"+refs/heads/{project_config.branch}:{ref}")
    history = run_repository_git(bare_repository, "rev-list", "--first-parent", "--reverse", ref).splitlines()
    start = run_repository_git(bare_repository, "rev-parse", "--verify", project_config.start_commit + "^{commit}")
    end = run_repository_git(bare_repository, "rev-parse", "--verify", project_config.end_commit + "^{commit}")
    if start not in history or end not in history or history.index(start) > history.index(end):
        raise ValueError("Start and end commits must be on the selected branch, with start no later than end")
    commits = history[history.index(start) : history.index(end) + 1]
    return bare_repository, commits if project_config.direction == "forward" else commits[::-1]
