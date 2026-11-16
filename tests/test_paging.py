"""Exercise page selection, real worktree state, CLI errors, and the CI guard."""

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

import pytest
import yaml

import debug_ic_fuzzer as runner
from impl.config import Compiler, Compilers, Project


@pytest.mark.parametrize(("start_at", "commit_count"), [(-1, None), (0, 0), (0, -1)])
def test_invalid_page_arguments(start_at, commit_count, tmp_path, monkeypatch):
    """Reject invalid bounds before reading configs or creating a run."""
    monkeypatch.setattr("impl.config.PROJECT_ROOT", tmp_path)
    with pytest.raises(ValueError, match="--start-at|--commit-count"):
        runner.replay_commits(Path("missing.yaml"), Path("missing.yaml"), start_at=start_at, commit_count=commit_count)
    assert not (tmp_path / "run").exists()


def test_cli_forwards_page_arguments(monkeypatch):
    """Exercise the public CLI rather than only its Python entry point."""
    calls = []
    monkeypatch.setattr(runner, "replay_commits", lambda *args, **kwargs: calls.append((args, kwargs)) or 0)
    for extra, expected in [([], dict(start_at=0, commit_count=None)), (["--start-at", "50", "--commit-count", "50"], dict(start_at=50, commit_count=50))]:
        monkeypatch.setattr(sys, "argv", ["debug-ic-fuzzer", "--compilers", "c.yaml", "--project", "p.yaml", *extra])
        assert runner.main() == 0
        assert calls[-1] == ((Path("c.yaml"), Path("p.yaml")), expected)


@pytest.mark.skipif(platform.system() != "Linux" or not shutil.which("cc") or not shutil.which("llvm-strip"), reason="Requires ELF build tools")
def test_pages_replay_real_git_and_isolate_incremental_state(tmp_path, monkeypatch):
    """Replay a five-commit Git fixture through real worktrees and Gradle subprocesses.

    A stand-in gradlew emits a real ELF product and a cache report. Selection and
    build-state isolation are real; this fixture does not claim to test Kotlin.
    """
    monkeypatch.setattr("impl.config.PROJECT_ROOT", tmp_path)
    source = tmp_path / "source"
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()

    (source / "value.c").write_text("int main(void) { return 0; }\n")
    subprocess.run(["cc", str(source / "value.c"), "-o", str(source / "program")], check=True)
    (source / "gradlew").write_text(
        f"#!{sys.executable}\n"
        + """import json, pathlib, shutil, sys
root = pathlib.Path.cwd()
home = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('-Pkotlin.native.home='))
cache = root / '.auto-cache'
with (cache / 'seen').open('a') as stream:
    stream.write((root / 'revision').read_text())
out = root / 'build/app.kexe'
out.parent.mkdir(exist_ok=True)
shutil.copyfile(root / 'program', out)
report = root / 'built-caches.txt'
report.write_text('')
print('IC_FUZZER ' + json.dumps(dict(task=':app:link', home=home, output=str(out), built_caches=str(report))))
print('> Task :app:link')
"""
    )
    (source / "gradlew").chmod(0o755)
    commits = []
    for index in range(5):
        (source / "revision").write_text(f"{index}\n")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", str(index))
        commits.append(git("rev-parse", "HEAD"))
    native_home = tmp_path / "compiler"
    (native_home / "bin").mkdir(parents=True)
    (native_home / "bin/konanc").touch()
    compilers = Compilers(compilers={name: Compiler(kotlin_version="2.4.21", kotlin_native_home=native_home) for name in ("baseline", "sut")})
    project = Project.model_construct(repository=str(source), branch="main", start_commit=commits[0], end_commit="latest", direction="forward", task=":app:link", gradle_args=[])
    monkeypatch.setattr(runner, "load_config", lambda path, model: compilers if model is Compilers else project)
    for direction, offset, count in [("forward", 0, 2), ("forward", 2, 2), ("forward", 4, 2), ("reverse", 1, 2), ("forward", 3, None)]:
        project.direction = direction
        before = set((tmp_path / "run").glob("*"))
        assert runner.replay_commits(Path("compilers.yaml"), Path("project.yaml"), start_at=offset, commit_count=count) == 0
        (run,) = set((tmp_path / "run").glob("*")) - before
        ordered = commits if direction == "forward" else commits[::-1]
        selected = ordered[offset : None if count is None else offset + count]
        assert json.loads((run / "range.json").read_text()) == dict(total_commits=5, start_at=offset, commit_count=len(selected), commits=selected)
        for index, commit in enumerate(selected):
            assert json.loads((run / f"{index:04d}-{commit[:12]}-comparison.json").read_text()) == dict(strict_equal=True, production_equal=True)
        for variant in ("baseline", "sut"):
            assert (run / variant / ".auto-cache/seen").read_text().splitlines() == [str(commits.index(commit)) for commit in selected]
        assert not (run / "project").exists()
    before = set((tmp_path / "run").iterdir())
    for offset in (5, 400):
        monkeypatch.setattr(sys, "argv", ["debug-ic-fuzzer", "--compilers", "c.yaml", "--project", "p.yaml", "--start-at", str(offset), "--commit-count", "50"])
        with pytest.raises(SystemExit) as error:
            runner.main()
        assert error.value.code == 2
    assert set((tmp_path / "run").iterdir()) == before


@pytest.mark.parametrize(
    ("guard", "status", "message", "expected"),
    [
        (True, 2, "--start-at 400 is outside the 356-commit range", 0),
        (True, 2, "network unavailable", 1),
        (True, 0, "page completed", 1),
        (True, 1, "build failed", 1),
        (False, 0, "page completed", 0),
        (False, 2, "--start-at 400 is outside the 356-commit range", 2),
    ],
)
def test_confetti_guard_accepts_only_empty_page(tmp_path, guard, status, message, expected):
    """Execute the actual workflow shell against explicit CLI outcomes."""
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/confetti.yml").read_text())
    entries = workflow["jobs"]["replay"]["strategy"]["matrix"]["include"]
    assert [entry["start_at"] for entry in entries if not entry.get("guard")] == list(range(0, 400, 50))
    assert entries[-1] == dict(start_at=400, guard=True)
    script = next(step["run"] for step in workflow["jobs"]["replay"]["steps"] if step.get("name") == "Replay page")
    python = tmp_path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$REPLAY_MESSAGE"\nexit "$REPLAY_STATUS"\n')
    python.chmod(0o755)
    result = subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path, env={**os.environ, "GUARD": str(guard).lower(), "START_AT": "400", "REPLAY_STATUS": str(status), "REPLAY_MESSAGE": message}, capture_output=True, text=True)
    assert result.returncode == expected, result.stdout + result.stderr
