from contextlib import ExitStack
import io
from pathlib import Path
import subprocess
import tarfile

import pytest
from pydantic import ValidationError

from debug_ic_fuzzer import commits
from ic_config import Compilers, Invalid, Project, prepare_home, within


def test_symlink_escape(tmp_path):
    tracked = tmp_path / 'tracked'
    tracked.mkdir()
    (tracked / 'escape').symlink_to(tmp_path)
    assert not within(tracked / 'escape/out.a', [tracked])
    assert not within(tmp_path / 'tracked-neighbor/out.a', [tracked])


def test_tar_traversal_rejected(tmp_path):
    path = tmp_path / 'bad.tgz'
    with tarfile.open(path, 'w:gz') as archive:
        info = tarfile.TarInfo('../escape')
        info.size = 1
        archive.addfile(info, io.BytesIO(b'x'))
    with ExitStack() as temporary, pytest.raises(tarfile.FilterError):
        prepare_home(path, temporary)


def test_configs_reject_unexpected_and_no_project_compiler_dependency():
    with pytest.raises(ValidationError):
        Project(repository='r', branch='main', start_commit='a', end_commit='b', task=':t', output='x')
    c = Compilers.model_validate({'compilers': {'baseline': {'kotlin_version': '2.4.21', 'kotlin_native_home': 'b'},
                                               'sut': {'kotlin_version': '2.4.21', 'kotlin_native_home': 's'}}})
    assert c.compilers['sut'].kotlin_native_home == Path('s')
    assert Project(repository='r', branch='main', start_commit='a', end_commit='b', task=':t').gradle_args == []
    with pytest.raises(ValidationError):
        Project(repository='r', branch='main', start_commit='a', end_commit='b', task=':t', gradle_args='-Pios=true')


def test_commit_range_and_reverse(tmp_path):
    def command(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args], text=True).strip()
    command('init', '-b', 'main')
    command('config', 'user.email', 'test@example.com')
    command('config', 'user.name', 'Test')
    revisions = []
    for value in ['a', 'b', 'c']:
        (tmp_path / 'file').write_text(value)
        command('add', 'file')
        command('commit', '-m', value)
        revisions.append(command('rev-parse', 'HEAD'))
    # clone 后非默认分支通常只有远端引用，没有同名本地分支。
    command('update-ref', 'refs/remotes/origin/topic', revisions[2])
    p = Project(repository='r', branch='topic', start_commit=revisions[0], end_commit=revisions[2], task=':t')
    assert commits(tmp_path, p) == revisions
    p.direction = 'reverse'
    assert commits(tmp_path, p) == revisions[::-1]
    p.start_commit, p.end_commit = revisions[2], revisions[0]
    with pytest.raises(Invalid):
        commits(tmp_path, p)
