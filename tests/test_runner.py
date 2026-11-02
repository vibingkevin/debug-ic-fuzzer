from contextlib import ExitStack, contextmanager
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from debug_ic_fuzzer import replay_commits
from impl.build import build_project
from impl.config import Compiler, Project, resolve_native_home
from impl.repository import activate_worktree, create_detached_worktree, prepare_repository


@pytest.fixture(autouse=True)
def isolate_tool_root(tmp_path, monkeypatch):
    """Set PROJECT_ROOT to tmp_path and run from an unrelated cwd using monkeypatch."""
    monkeypatch.setattr("impl.config.PROJECT_ROOT", tmp_path)
    caller = tmp_path / "unrelated-cwd"
    caller.mkdir()
    monkeypatch.chdir(caller)


@pytest.mark.parametrize("extension", ["tgz", "tar.gz"])
def test_home_paths_and_fresh_archives(tmp_path, monkeypatch, extension):
    """Check home resolution and archive cleanup using tmp_path for distributions.

    monkeypatch fixes the host and KONAN_DATA_DIR; extension selects tgz or tar.gz.
    """
    monkeypatch.setattr("impl.config.platform.system", lambda: "Linux")
    monkeypatch.setattr("impl.config.platform.machine", lambda: "x86_64")
    monkeypatch.setenv("KONAN_DATA_DIR", str(tmp_path / "konan"))
    archive = tmp_path / f"compiler.{extension}"
    with tarfile.open(archive, "w:gz") as tar:
        entry = tarfile.TarInfo("dist/bin/konanc")
        entry.size = 1
        tar.addfile(entry, io.BytesIO(b"x"))
    with ExitStack() as stack:
        assert resolve_native_home(Compiler(kotlin_version="2.4.21"), tmp_path, stack) == tmp_path / ".konan/kotlin-native-prebuilt-linux-x86_64-2.4.21"
        with pytest.raises(ValueError, match="absolute path"):
            resolve_native_home(Compiler(kotlin_version="2.4.21", kotlin_native_home="relative"), tmp_path, stack)
        c = Compiler(kotlin_version="2.4.21", kotlin_native_home=f"compiler.{extension}")
        first, second = [resolve_native_home(c, tmp_path, stack) for _ in range(2)]
        assert first != second and (first / "bin/konanc").is_file()
    assert not first.exists() and not second.exists()


def test_tar_cannot_escape(tmp_path):
    """Use a malicious archive in tmp_path to verify extraction rejects parent traversal."""
    archive = tmp_path / "bad.tgz"
    with tarfile.open(archive, "w:gz") as tar:
        entry = tarfile.TarInfo("../escape")
        entry.size = 1
        tar.addfile(entry, io.BytesIO(b"x"))
    with ExitStack() as stack, pytest.raises(tarfile.FilterError):
        resolve_native_home(Compiler(kotlin_version="2.4.21", kotlin_native_home=archive), tmp_path, stack)
    assert not (tmp_path / "escape").exists()


def test_same_named_repositories_use_separate_remotes(tmp_path):
    """Create two sample repositories under tmp_path and verify remote reuse and branch isolation."""
    projects = []
    for owner in ("first", "second"):
        source = tmp_path / owner / "sample"
        subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "--allow-empty", "-m", owner], check=True, capture_output=True)
        commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        projects.append(Project.model_construct(repository=str(source), branch="main", start_commit=commit, end_commit=commit, direction="forward"))

    repository = tmp_path / "repos/sample.git"
    for project in projects + projects:
        assert prepare_repository(tmp_path / "repos", project) == (repository, [project.start_commit])
    remotes = subprocess.check_output(["git", "--git-dir", str(repository), "remote"], text=True).splitlines()
    assert remotes == ["origin", "remote-1"]
    for remote, project in zip(remotes, projects):
        assert subprocess.check_output(["git", "--git-dir", str(repository), "rev-parse", f"refs/remotes/{remote}/main"], text=True).strip() == project.start_commit

    # A commit fetched from another remote is not part of this project's branch.
    projects[0].start_commit = projects[1].start_commit
    with pytest.raises(ValueError, match="selected branch"):
        prepare_repository(tmp_path / "repos", projects[0])


def test_shared_repository_independent_worktrees_and_failed_build(tmp_path, monkeypatch):
    """Check commit selection, worktree restoration, and build failures in tmp_path.

    monkeypatch isolates cwd and supplies an external Gradle home to test its override.
    """
    monkeypatch.setenv("GRADLE_USER_HOME", str(tmp_path / "external-home"))
    monkeypatch.setenv("KONAN_DATA_DIR", str(tmp_path / "external-konan"))
    source = tmp_path / "source"
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)

    def run_source_git(*git_args):
        """Run Git git_args in the fixture's source repository and return stripped stdout."""
        return subprocess.check_output(["git", "-C", str(source), *git_args], text=True).strip()

    # Use a fake local gradlew to check process execution, moves, failure logs, and arguments.
    (source / "gradlew").write_text(
        f"#!{sys.executable}\n"
        + """import json, os, pathlib, sys
args = sys.argv[1:]
assert pathlib.Path(os.environ['KONAN_DATA_DIR']) == pathlib.Path(args[args.index('--gradle-user-home') + 1]).parent / '.konan'
assert os.environ['ZERO_AR_DATE'] == '1'
home = next((a.split('=', 1)[1] for a in args if a.startswith('-Pkotlin.native.home=')), '/downloaded')
out = pathlib.Path.cwd() / 'build/app.kexe'
out.parent.mkdir(exist_ok=True)
out.write_text('output')
print('IC_FUZZER ' + json.dumps(dict(home=home, output=str(out))))
print('-Xic-cache-dir=' + str(pathlib.Path.cwd() / 'build/ic'))
sys.exit(7 if home.endswith('fail') else 0)
"""
    )
    (source / "gradlew").chmod(0o755)
    run_source_git("add", ".")
    run_source_git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "first")
    first = run_source_git("rev-parse", "HEAD")
    (source / "second").write_text("second")
    run_source_git("add", ".")
    run_source_git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "second")
    second = run_source_git("rev-parse", "HEAD")
    # Bypass URL validation for the local Git fixture; the CLI still requires a remote URL.
    project = Project.model_construct(repository=str(source), branch="main", start_commit=first, end_commit=second, direction="forward", task=":app:link", gradle_args=[])
    repository, revisions = prepare_repository(tmp_path / "repos", project)
    assert revisions == [first, second]
    project.direction = "reverse"
    assert prepare_repository(tmp_path / "repos", project) == (repository, [second, first])
    project.end_commit = first
    assert prepare_repository(tmp_path / "repos", project)[1] == [first]
    project.start_commit = second
    with pytest.raises(ValueError, match="start no later than end"):
        prepare_repository(tmp_path / "repos", project)
    project.start_commit = "missing-commit"
    with pytest.raises(ValueError):
        prepare_repository(tmp_path / "repos", project)
    run_source_git("checkout", "-b", "side", first)
    (source / "side").write_text("side")
    run_source_git("add", ".")
    run_source_git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "side")
    project.start_commit = run_source_git("rev-parse", "HEAD")
    run_source_git("checkout", "main")
    subprocess.run(["git", "--git-dir", str(repository), "fetch", "origin", "side"], check=True, capture_output=True)
    with pytest.raises(ValueError, match="selected branch"):
        prepare_repository(tmp_path / "repos", project)
    project.start_commit, project.end_commit = first, second
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    for name, sha in [("baseline", first), ("sut", second)]:
        create_detached_worktree(repository, run_directory / name, sha)
    for name, home in [("baseline", Path("/compiler/fail")), ("sut", Path("/downloaded"))]:
        compiler = Compiler(kotlin_version="2.4.21", kotlin_native_home=home if name == "baseline" else None)
        with activate_worktree(repository, run_directory / name, first) as active:
            result = build_project(active, project, compiler, home, f"0000-{first[:12]}-{name}")
            (active / ".auto-cache/owner").write_text(name)
        assert result["exit_code"] == (7 if name == "baseline" else 0)
        assert result["error"] is None
        assert result["log"] == f"0000-{first[:12]}-{name}.gradle.log"
        assert (run_directory / result["log"]).is_file()
        assert not (run_directory / "store").exists()
        assert not (run_directory / "project").exists()
        assert "-Pic.kotlin.version=2.4.21" in result["command"]
        assert f"-Pic.auto.cache.dir={run_directory}/project/.auto-cache" in result["command"]
        assert "--info" not in result["command"]
        assert result["command"][result["command"].index("--gradle-user-home") + 1] == str(tmp_path / ".gradle-home")
        assert "-Pkotlin.internal.compiler.arguments.log.level=warning" in result["command"]
        assert not any(a.startswith("-Pkotlin.native.version=") for a in result["command"])
        assert not any(a.startswith("--no-") for a in result["command"])
    assert subprocess.check_output(["git", "-C", str(run_directory / "baseline"), "rev-parse", "HEAD"], text=True).strip() == first
    assert subprocess.check_output(["git", "-C", str(run_directory / "sut"), "rev-parse", "HEAD"], text=True).strip() == first

    assert (run_directory / "baseline/.auto-cache/owner").read_text() == "baseline"
    assert (run_directory / "sut/.auto-cache/owner").read_text() == "sut"

    # Reuse a worktree for the next commit without losing its build outputs.
    with activate_worktree(repository, run_directory / "sut", second) as worktree:
        assert (worktree / ".auto-cache/owner").read_text() == "sut"
        assert (worktree / "second").read_text() == "second"
        assert (worktree / "build/app.kexe").exists()
        assert subprocess.check_output(["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True).strip() == second
    assert not (run_directory / "project").exists()
    with pytest.raises(subprocess.CalledProcessError):
        with activate_worktree(repository, run_directory / "sut", "missing-commit"):
            pytest.fail("Invalid commit must not enter the build phase")
    assert (run_directory / "sut").is_dir()
    assert not (run_directory / "project").exists()


@pytest.mark.parametrize("failure", [None, "baseline", "sut", "production", "debug", "collection", "both-up-to-date", "baseline-up-to-date", "sut-up-to-date"])
def test_replay_order_records_and_stop(tmp_path, monkeypatch, failure):
    """Verify replay order and stopping with fake builds writing records under tmp_path.

    monkeypatch replaces external operations; failure selects the injected error/difference,
    or None for success. Both variants must finish the current commit before stopping.
    """
    import debug_ic_fuzzer as runner
    from impl.config import Compilers
    from impl.comparison import compare_products

    compilers = Compilers(compilers={name: Compiler(kotlin_version="2.4.21") for name in ("baseline", "sut")})
    project = Project(repository="https://example.test/project.git", branch="main", start_commit="a", end_commit="c", task=":app:link", gradle_args=["-Pfixture={config_dir}/fixture"])
    monkeypatch.setattr(runner, "load_config", lambda path, model: compilers if model is Compilers else project)
    monkeypatch.setattr(runner, "resolve_native_home", lambda *_: Path("/shared-compiler"))
    monkeypatch.setattr(runner, "prepare_repository", lambda *_: (tmp_path / "repo", ["a", "b", "c"]))
    monkeypatch.setattr("impl.summary.read_commit_change_stats", lambda *_: dict(changed_files=1, added_lines=2, deleted_lines=1))
    created, calls, collected, saved = [], [], [], []
    active = False

    @contextmanager
    def fake_activate_worktree(bare_repository, parked_worktree, commit_id):
        """Yield parked_worktree's sibling project path and track its active lifetime.

        bare_repository and commit_id are unused arguments matching activate_worktree.
        """
        nonlocal active
        active = True
        try:
            yield parked_worktree.parent / "project"
        finally:
            active = False

    monkeypatch.setattr(runner, "activate_worktree", fake_activate_worktree)
    monkeypatch.setattr(runner, "create_detached_worktree", lambda bare_repository, worktree_directory, commit_id: created.append((worktree_directory.name, commit_id)))

    def fake_build_project(project_directory, project_config, compiler_config, native_home, record_stem):
        """Write a fake build log beside project_directory and return an injected build result.

        record_stem identifies the test commit and variant. project_config supplies
        resolved Gradle arguments to check. compiler_config
        and native_home are unused arguments matching build_project.
        """
        assert active
        _, commit_id, variant = record_stem.split("-")
        calls.append((commit_id, variant))
        assert project_config.gradle_args == [f"-Pfixture={tmp_path}/fixture"]
        (project_directory.parent / f"{record_stem}.gradle.log").write_text("completed")
        skipped = commit_id == "b" and failure in ("both-up-to-date", f"{variant}-up-to-date")
        return dict(cwd=str(project_directory), exit_code=int(commit_id == "b" and variant == failure), error=None, task_state="UP-TO-DATE" if skipped else "EXECUTED", link=None if skipped else {"task": ":app:link"})

    monkeypatch.setattr(runner, "build_project", fake_build_project)

    def fake_collect_cache_files(build_result, store_directory):
        """Check active collection and inject the selected cache failure.

        build_result and store_directory are unused inputs matching collect_cache_files.
        """
        assert active
        collected.append(calls[-1])
        if failure == "collection" and calls[-1] == ("b", "baseline"):
            raise ValueError("Cache file cannot be read")
        return []

    def fake_collect_product_hashes(build_result):
        """Verify build_result already has commit/variant metadata while the project is active."""
        assert active
        assert (build_result["commit"], build_result["variant"]) == calls[-1]

    def fake_compare_products(variant_results):
        """Use variant_results' baseline commit to inject differences after worktrees are parked."""
        assert not active
        if any(result["task_state"] == "UP-TO-DATE" for result in variant_results.values()):
            return compare_products(variant_results)
        changed = failure in ("production", "debug") and variant_results["baseline"]["commit"] == "b"
        return dict(strict_equal=not changed, production_equal=not (changed and failure == "production"))

    monkeypatch.setattr(runner, "collect_cache_files", fake_collect_cache_files)
    monkeypatch.setattr(runner, "collect_product_hashes", fake_collect_product_hashes)
    monkeypatch.setattr(runner, "save_product_contents", lambda *_: None)
    monkeypatch.setattr("impl.comparison.save_product_contents", lambda results: saved.append(results))
    monkeypatch.setattr(runner, "compare_products", fake_compare_products)
    assert replay_commits(Path("compilers.yaml"), Path("project.yaml")) == int(failure not in (None, "debug", "both-up-to-date"))
    assert created == [("baseline", "a"), ("sut", "a")]
    revisions = ["a", "b"] if failure not in (None, "debug", "both-up-to-date") else ["a", "b", "c"]
    assert calls == [(sha, name) for sha in revisions for name in ("baseline", "sut")]
    (run_directory,) = (tmp_path / "run").iterdir()
    assert len(list(run_directory.glob("*.json"))) == len(calls) + len(revisions)
    assert (run_directory / "summary.csv").is_file()
    for index, sha in enumerate(revisions):
        for name in ("baseline", "sut"):
            record_name = f"{index:04d}-{sha}-{name}"
            record = json.loads((run_directory / f"{record_name}.json").read_text())
            assert record["commit"] == sha
            if record["task_state"] == "UP-TO-DATE":
                assert record["link"] is record["cache_files"] is record["artifacts"] is None
                assert (sha, name) not in collected
            else:
                assert (sha, name) in collected
            assert (run_directory / f"{record_name}.gradle.log").is_file()
    if failure in ("both-up-to-date", "baseline-up-to-date", "sut-up-to-date"):
        comparison = json.loads((run_directory / "0001-b-comparison.json").read_text())
        if failure == "both-up-to-date":
            assert comparison["strict_equal"] is comparison["production_equal"] is None
            assert "skipped" in comparison
            assert not saved
        else:
            assert "only one variant is UP-TO-DATE" in comparison["error"]
            assert len(saved) == 1


@pytest.mark.parametrize(
    "task_line,expected_state,error",
    [("> Task :app:link UP-TO-DATE", "UP-TO-DATE", None), ("> Task :app:link FROM-CACHE", "FROM-CACHE", "Build Cache"), ("> Task :app:link", "EXECUTED", "Expected one Native link"), ("> Task :other:task", None, "Expected one Native link")],
)
def test_task_state_without_link(tmp_path, monkeypatch, task_line, expected_state, error):
    """Check task-state parsing with no link metadata or existing products in tmp_path.

    monkeypatch replaces Gradle execution; task_line is its output, expected_state is the
    parsed state, and error is the expected validation message fragment or None.
    """
    import importlib

    builder = importlib.import_module("impl.build")

    def fake_gradle(command, cwd, stdout, **kwargs):
        """Write task_line to stdout and return success for command; cwd/kwargs are unused process options."""
        stdout.write(task_line + "\n")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(builder.subprocess, "run", fake_gradle)
    (tmp_path / "project").mkdir()
    project = Project(repository="https://example.test/project.git", branch="main", start_commit="a", end_commit="b", task=":app:link")
    result = build_project(tmp_path / "project", project, Compiler(kotlin_version="2.4.21"), Path("/compiler"), "0001-b-baseline")
    assert result["task_state"] == expected_state
    assert result["link"] is None
    if error is None:
        assert result["error"] is None
    else:
        assert error in result["error"]
