from contextlib import ExitStack
import io
from pathlib import Path
import subprocess
import tarfile

import pytest
from pydantic import ValidationError

from debug_ic_fuzzer import commits
from impl.config import Compilers, Invalid, Project, prepare_home, within


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
        Project(repository='https://example.test/sample.git', branch='main', start_commit='a', end_commit='b', task=':t', output='x')
    c = Compilers.model_validate({'compilers': {'baseline': {'kotlin_version': '2.4.21', 'kotlin_native_home': 'b'},
                                               'sut': {'kotlin_version': '2.4.21', 'kotlin_native_home': 's'}}})
    assert c.compilers['sut'].kotlin_native_home == Path('s')
    assert Project(repository='https://example.test/sample.git', branch='main', start_commit='a', end_commit='b', task=':t').gradle_args == []
    with pytest.raises(ValidationError):
        Project(repository='https://example.test/sample.git', branch='main', start_commit='a', end_commit='b', task=':t', gradle_args='-Pios=true')


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
    p = Project(repository='https://example.test/sample.git', branch='topic', start_commit=revisions[0], end_commit=revisions[2], task=':t')
    assert commits(tmp_path, p) == revisions
    p.direction = 'reverse'
    assert commits(tmp_path, p) == revisions[::-1]
    p.start_commit, p.end_commit = revisions[2], revisions[0]
    with pytest.raises(Invalid):
        commits(tmp_path, p)


def test_config_dir_placeholder(tmp_path, monkeypatch):
    from impl.config import expand_args
    base = tmp_path / 'config with spaces'
    monkeypatch.chdir(tmp_path)
    args = ['-I', '{config_dir}/test.gradle', '--init-script={config_dir}/other.gradle', '-Pvalue={literal}']
    resolved = expand_args(args, base)
    assert resolved == ['-I', str(base / 'test.gradle'), f'--init-script={base}/other.gradle', '-Pvalue={literal}']
    assert args[1] == '{config_dir}/test.gradle'


def test_one_project_and_one_task():
    values = dict(repository='https://example.test/sample.git', branch='main', start_commit='a', end_commit='b', task=':app:link')
    assert Project.model_validate(values).task == ':app:link'
    with pytest.raises(ValidationError):
        Project.model_validate({'projects': [values, values]})
    with pytest.raises(ValidationError):
        Project.model_validate(values | {'task': ':a:link :b:link'})


@pytest.mark.parametrize('extension', ['yaml', 'yml'])
def test_cli_records_failure_in_named_run_and_preserves_previous(tmp_path, monkeypatch, extension):
    import json
    import sys
    from debug_ic_fuzzer import main
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['debug-ic-fuzzer',
        '--compilers', f'configs/compiler pair.{extension}',
        '--projects', f'configs/sample.{extension}'])
    # 缺少配置也应把错误记录到推导出的目录；重跑不能覆盖已有结果。
    assert main() == 2
    error = tmp_path / 'runs/compiler pair-sample/error.json'
    assert json.loads(error.read_text())['status'] == 'INVALID'
    error.write_text('previous result')
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert error.read_text() == 'previous result'


@pytest.mark.parametrize('system,arch,folder', [
    ('Linux', 'x86_64', 'linux-x86_64'),
    ('Darwin', 'arm64', 'macos-aarch64'),
])
@pytest.mark.parametrize('custom_root', [False, True])
def test_default_native_home(tmp_path, monkeypatch, system, arch, folder, custom_root):
    from impl.config import Compiler, default_home
    monkeypatch.setattr('impl.config.platform.system', lambda: system)
    monkeypatch.setattr('impl.config.platform.machine', lambda: arch)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    if custom_root:
        monkeypatch.setenv('KONAN_DATA_DIR', str(tmp_path / 'custom konan'))
    else:
        monkeypatch.delenv('KONAN_DATA_DIR', raising=False)
    compiler = Compiler(kotlin_version='2.4.21')
    assert compiler.kotlin_native_home is None
    root = tmp_path / ('custom konan' if custom_root else '.konan')
    assert default_home(compiler.kotlin_version) == root / f'kotlin-native-prebuilt-{folder}-2.4.21'


def test_same_default_native_cache_rejected(tmp_path):
    from debug_ic_fuzzer import run
    compilers = tmp_path / 'compilers.yaml'
    compilers.write_text('compilers:\n  baseline: {kotlin_version: "2.4.21"}\n  sut: {kotlin_version: "2.4.21"}\n')
    projects = tmp_path / 'projects.yaml'
    projects.write_text('{repository: https://example.test/sample.git, branch: main, start_commit: a, end_commit: b, task: ":link"}')
    with pytest.raises(Invalid, match='不同的编译器目录'):
        run(compilers, projects, tmp_path / 'run')


@pytest.mark.parametrize('repository', ['../sample', '/tmp/sample', 'file:///tmp/sample'])
def test_repository_rejects_local_paths(repository):
    with pytest.raises(ValidationError):
        Project(repository=repository, branch='main', start_commit='a', end_commit='b', task=':link')


def test_reuse_repository_update_branch_and_keep_changes(tmp_path, monkeypatch):
    from debug_ic_fuzzer import checkouts, git
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init', '-b', 'main')
    git(source, 'config', 'user.email', 'test@example.com')
    git(source, 'config', 'user.name', 'Test')
    (source / 'file').write_text('first')
    git(source, 'add', 'file')
    git(source, 'commit', '-m', 'first')
    url = 'https://example.test/sample.git'
    # 使用真实 Git 的 URL 重写模拟远端，产品配置仍只接受 URL。
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', f'url.{source.as_uri()}.insteadOf')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', url)
    monkeypatch.setattr('debug_ic_fuzzer.REPOSITORIES', tmp_path / 'repos')
    project = Project(repository=url, branch='main', start_commit='HEAD', end_commit='HEAD', task=':link')
    with checkouts(project) as (baseline, sut):
        inode = (baseline / '.git').stat().st_ino
        assert (sut / 'file').read_text() == 'first'
        with pytest.raises(Invalid, match='另一次运行'):
            with checkouts(project):
                pass
        git(baseline, 'checkout', '--detach', 'HEAD')
    (source / 'file').write_text('updated')
    git(source, 'commit', '-am', 'second')
    with checkouts(project) as (again, sut):
        assert again == baseline and (again / '.git').stat().st_ino == inode
        assert (again / 'file').read_text() == (sut / 'file').read_text() == 'updated'
        assert git(again, 'rev-parse', 'HEAD') == git(source, 'rev-parse', 'HEAD')
    git(source, 'checkout', '-b', 'topic')
    (source / 'file').write_text('topic')
    git(source, 'commit', '-am', 'topic')
    project.branch = 'topic'
    with checkouts(project) as (again, sut):
        assert (again / '.git').stat().st_ino == inode
        assert (again / 'file').read_text() == (sut / 'file').read_text() == 'topic'
    (baseline / 'file').write_text('user changes')
    with pytest.raises(Invalid, match='未提交修改'):
        with checkouts(project):
            pass
    assert (baseline / 'file').read_text() == 'user changes'

    (baseline.parent / 'project').mkdir()
    with pytest.raises(Invalid, match='尚未恢复'):
        with checkouts(project):
            pass
    assert (baseline / 'file').read_text() == 'user changes'
