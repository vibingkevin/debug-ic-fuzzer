from contextlib import ExitStack
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from debug_ic_fuzzer import run
from impl.build import build
from impl.config import Compiler, Project, prepare_home
from impl.repository import checkout_worktree, create_worktree, prepare_repository


@pytest.mark.parametrize("extension", ["tgz", "tar.gz"])
def test_home_paths_and_fresh_archives(tmp_path, monkeypatch, extension):
    monkeypatch.setattr('impl.config.platform.system', lambda: 'Linux')
    monkeypatch.setattr('impl.config.platform.machine', lambda: 'x86_64')
    monkeypatch.setenv('KONAN_DATA_DIR', str(tmp_path / 'konan'))
    archive = tmp_path / f'compiler.{extension}'
    with tarfile.open(archive, 'w:gz') as tar:
        entry = tarfile.TarInfo('dist/bin/konanc')
        entry.size = 1
        tar.addfile(entry, io.BytesIO(b'x'))
    with ExitStack() as stack:
        assert prepare_home(Compiler(kotlin_version='2.4.21'), tmp_path, stack) == tmp_path / 'konan/kotlin-native-prebuilt-linux-x86_64-2.4.21'
        with pytest.raises(ValueError, match='absolute path'):
            prepare_home(Compiler(kotlin_version='2.4.21', kotlin_native_home='relative'), tmp_path, stack)
        c = Compiler(kotlin_version='2.4.21', kotlin_native_home=f'compiler.{extension}')
        first, second = [prepare_home(c, tmp_path, stack) for _ in range(2)]
        assert first != second and (first / 'bin/konanc').is_file()
    assert not first.exists() and not second.exists()


def test_tar_cannot_escape(tmp_path):
    archive = tmp_path / 'bad.tgz'
    with tarfile.open(archive, 'w:gz') as tar:
        entry = tarfile.TarInfo('../escape')
        entry.size = 1
        tar.addfile(entry, io.BytesIO(b'x'))
    with ExitStack() as stack, pytest.raises(tarfile.FilterError):
        prepare_home(Compiler(kotlin_version='2.4.21', kotlin_native_home=archive), tmp_path, stack)
    assert not (tmp_path / 'escape').exists()


def test_same_default_home_fails_before_clone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'compilers.yaml').write_text('compilers:\n  baseline: {kotlin_version: "2.4.21"}\n  sut: {kotlin_version: "2.4.21"}\n')
    (tmp_path / 'project.yaml').write_text('repository: https://example.test/project.git\nbranch: main\nstart_commit: a\nend_commit: a\ntask: :app:link\n')
    with pytest.raises(ValueError, match='same Native home'):
        run(tmp_path / 'compilers.yaml', tmp_path / 'project.yaml')
    assert not (tmp_path / 'repos').exists()


def test_shared_repository_independent_worktrees_and_failed_build(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRADLE_USER_HOME", str(tmp_path / "external-home"))
    source = tmp_path / 'source'
    subprocess.run(['git', 'init', '-b', 'main', str(source)], check=True, capture_output=True)
    def local(*args):
        return subprocess.check_output(['git', '-C', str(source), *args], text=True).strip()
    # Use a fake local gradlew to check process execution, moves, failure logs, and arguments.
    (source / 'gradlew').write_text(f'#!{sys.executable}\n' + '''import json, pathlib, sys
args = sys.argv[1:]
home = next((a.split('=', 1)[1] for a in args if a.startswith('-Pkotlin.native.home=')), '/downloaded')
out = pathlib.Path.cwd() / 'build/app.kexe'
out.parent.mkdir(exist_ok=True)
out.write_text('output')
print('IC_FUZZER ' + json.dumps(dict(home=home, output=str(out))))
sys.exit(7 if home.endswith('fail') else 0)
''')
    (source / 'gradlew').chmod(0o755)
    local('add', '.')
    local('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-m', 'first')
    first = local('rev-parse', 'HEAD')
    (source / 'second').write_text('second')
    local('add', '.')
    local('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-m', 'second')
    second = local('rev-parse', 'HEAD')
    # Bypass URL validation for the local Git fixture; the CLI still requires a remote URL.
    project = Project.model_construct(repository=str(source), branch='main', start_commit=first,
                                      end_commit=second, direction='forward', task=':app:link', gradle_args=[])
    repository, revisions = prepare_repository(tmp_path / 'repos', project)
    assert revisions == [first, second]
    project.direction = 'reverse'
    assert prepare_repository(tmp_path / 'repos', project) == (repository, [second, first])
    project.end_commit = first
    assert prepare_repository(tmp_path / 'repos', project)[1] == [first]
    project.start_commit = second
    with pytest.raises(ValueError, match='start no later than end'):
        prepare_repository(tmp_path / 'repos', project)
    project.start_commit = 'missing-commit'
    with pytest.raises(ValueError):
        prepare_repository(tmp_path / 'repos', project)
    local('checkout', '-b', 'side', first)
    (source / 'side').write_text('side')
    local('add', '.')
    local('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-m', 'side')
    project.start_commit = local('rev-parse', 'HEAD')
    local('checkout', 'main')
    subprocess.run(['git', '--git-dir', str(repository), 'fetch', 'origin', 'side'], check=True, capture_output=True)
    with pytest.raises(ValueError, match='selected branch'):
        prepare_repository(tmp_path / 'repos', project)
    project.start_commit, project.end_commit = first, second
    destination = tmp_path / 'run'
    destination.mkdir()
    for name, sha in [('baseline', first), ('sut', second)]:
        create_worktree(repository, destination / name, sha)
    for name, home in [('baseline', Path('/compiler/fail')), ('sut', Path('/downloaded'))]:
        compiler = Compiler(kotlin_version='2.4.21', kotlin_native_home=home if name == 'baseline' else None)
        result = build(repository, destination, name, first, project, compiler, home, tmp_path, f'0000-{first[:12]}-{name}')
        assert result['exit_code'] == (7 if name == 'baseline' else 0)
        assert result['error'] is None
        assert result['log'] == f'0000-{first[:12]}-{name}.gradle.log'
        assert (destination / result['log']).is_file()
        assert not (destination / 'store').exists()
        assert not (destination / 'project').exists()
        assert '-Pic.kotlin.version=2.4.21' in result['command']
        assert '--info' not in result['command']
        assert result['command'][result['command'].index('--gradle-user-home') + 1] == str(tmp_path / '.gradle-home')
        assert '-Pkotlin.internal.compiler.arguments.log.level=warning' in result['command']
        assert not any(a.startswith('-Pkotlin.native.version=') for a in result['command'])
        assert not any(a.startswith('--no-') for a in result['command'])
    assert subprocess.check_output(['git', '-C', str(destination / 'baseline'), 'rev-parse', 'HEAD'], text=True).strip() == first
    assert subprocess.check_output(['git', '-C', str(destination / 'sut'), 'rev-parse', 'HEAD'], text=True).strip() == first

    # Reuse a worktree for the next commit without losing its build outputs.
    with checkout_worktree(repository, destination / 'sut', second) as worktree:
        assert (worktree / 'second').read_text() == 'second'
        assert (worktree / 'build/app.kexe').exists()
        assert subprocess.check_output(['git', '-C', str(worktree), 'rev-parse', 'HEAD'], text=True).strip() == second
    assert not (destination / 'project').exists()
    with pytest.raises(subprocess.CalledProcessError):
        with checkout_worktree(repository, destination / 'sut', 'missing-commit'):
            pytest.fail('Invalid commit must not enter the build phase')
    assert (destination / 'sut').is_dir()
    assert not (destination / 'project').exists()


@pytest.mark.parametrize("failure", [None, "baseline", "sut"])
def test_replay_order_records_and_stop(tmp_path, monkeypatch, failure):
    import debug_ic_fuzzer as runner
    from impl.config import Compilers

    monkeypatch.chdir(tmp_path)
    compilers = Compilers(compilers={name: Compiler(kotlin_version='2.4.21') for name in ('baseline', 'sut')})
    project = Project(repository='https://example.test/project.git', branch='main',
                      start_commit='a', end_commit='c', task=':app:link')
    monkeypatch.setattr(runner, 'load', lambda path, model: compilers if model is Compilers else project)
    monkeypatch.setattr(runner, 'prepare_home', lambda compiler, *_: Path('/baseline') if compiler is compilers.compilers['baseline'] else Path('/sut'))
    monkeypatch.setattr(runner, 'prepare_repository', lambda *_: (tmp_path / 'repo', ['a', 'b', 'c']))
    created, calls = [], []
    monkeypatch.setattr(runner, 'create_worktree', lambda repo, path, sha: created.append((path.name, sha)))

    def fake_build(repo, destination, name, revision, project, compiler, home, config_dir, record_name, last_link):
        calls.append((revision, name))
        assert last_link == (None if revision == 'a' else {'task': ':app:link'})
        (destination / f'{record_name}.gradle.log').write_text('completed')
        return dict(commit=revision, variant=name, exit_code=int(revision == 'b' and name == failure),
                    error=None, link={'task': ':app:link'})

    monkeypatch.setattr(runner, 'build', fake_build)
    assert run(tmp_path / 'compilers.yaml', tmp_path / 'project.yaml') == int(failure is not None)
    assert created == [('baseline', 'a'), ('sut', 'a')]
    revisions = ['a', 'b'] if failure else ['a', 'b', 'c']
    assert calls == [(sha, name) for sha in revisions for name in ('baseline', 'sut')]
    destination, = (tmp_path / 'run').iterdir()
    assert len(list(destination.glob('*.json'))) == len(calls)
    for index, sha in enumerate(revisions):
        for name in ('baseline', 'sut'):
            record_name = f'{index:04d}-{sha}-{name}'
            assert json.loads((destination / f'{record_name}.json').read_text())['commit'] == sha
            assert (destination / f'{record_name}.gradle.log').is_file()


@pytest.mark.parametrize('state,previous,output,valid', [
    ('UP-TO-DATE', True, True, True),
    ('UP-TO-DATE', False, True, False),
    ('UP-TO-DATE', True, False, False),
    ('FROM-CACHE', True, True, False),
])
def test_skipped_link(tmp_path, monkeypatch, state, previous, output, valid):
    from contextlib import contextmanager
    import importlib
    builder = importlib.import_module('impl.build')

    @contextmanager
    def checkout(*_):
        yield tmp_path / 'project'

    def gradle(command, cwd, stdout, **kwargs):
        stdout.write(f'> Task :app:link {state}\n')
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(builder, 'checkout_worktree', checkout)
    monkeypatch.setattr(builder.subprocess, 'run', gradle)
    (tmp_path / 'baseline').mkdir()
    if output:
        (tmp_path / 'baseline/app.kexe').touch()
    link = dict(home='/compiler', output=str(tmp_path / 'project/app.kexe'))
    project = Project(repository='https://example.test/project.git', branch='main',
                      start_commit='a', end_commit='b', task=':app:link')
    result = build(tmp_path, tmp_path, 'baseline', 'b', project, Compiler(kotlin_version='2.4.21'),
                   Path('/compiler'), tmp_path, '0001-b-baseline', link if previous else None)
    assert (result['error'] is None) == valid
    assert result['task_state'] == state
