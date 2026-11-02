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
from impl.repository import git, prepare_repository


def test_home_paths_and_fresh_archives(tmp_path, monkeypatch):
    monkeypatch.setattr('impl.config.platform.system', lambda: 'Linux')
    monkeypatch.setattr('impl.config.platform.machine', lambda: 'x86_64')
    monkeypatch.setenv('KONAN_DATA_DIR', str(tmp_path / 'konan'))
    archive = tmp_path / 'compiler.tgz'
    with tarfile.open(archive, 'w:gz') as tar:
        entry = tarfile.TarInfo('dist/bin/konanc')
        entry.size = 1
        tar.addfile(entry, io.BytesIO(b'x'))
    with ExitStack() as stack:
        assert prepare_home(Compiler(kotlin_version='2.4.21'), tmp_path, stack) == tmp_path / 'konan/kotlin-native-prebuilt-linux-x86_64-2.4.21'
        with pytest.raises(ValueError, match='absolute path'):
            prepare_home(Compiler(kotlin_version='2.4.21', kotlin_native_home='relative'), tmp_path, stack)
        c = Compiler(kotlin_version='2.4.21', kotlin_native_home='compiler.tgz')
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


def test_shared_repository_independent_worktrees_and_failed_build(tmp_path):
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
    repository, revision = prepare_repository(tmp_path / 'repos', project)
    assert revision == first
    project.direction = 'reverse'
    assert prepare_repository(tmp_path / 'repos', project) == (repository, second)
    destination = tmp_path / 'run'
    destination.mkdir()
    for name, sha in [('baseline', first), ('sut', second)]:
        git(repository, 'worktree', 'add', '--detach', str(destination / name), sha)
    for name, home in [('baseline', Path('/compiler/fail')), ('sut', Path('/downloaded'))]:
        compiler = Compiler(kotlin_version='2.4.21', kotlin_native_home=home if name == 'baseline' else None)
        result = build(repository, destination, name, revision, project, compiler, home, tmp_path)
        assert result['exit_code'] == (7 if name == 'baseline' else 0)
        assert result['error'] is None
        assert (destination / result['log']).is_file()
        assert not (destination / 'project').exists()
        assert '-Pic.kotlin.version=2.4.21' in result['command']
        assert not any(a.startswith('-Pkotlin.native.version=') for a in result['command'])
        assert not any(a.startswith('--no-') for a in result['command'])
    assert subprocess.check_output(['git', '-C', str(destination / 'baseline'), 'rev-parse', 'HEAD'], text=True).strip() == first
    assert subprocess.check_output(['git', '-C', str(destination / 'sut'), 'rev-parse', 'HEAD'], text=True).strip() == second
