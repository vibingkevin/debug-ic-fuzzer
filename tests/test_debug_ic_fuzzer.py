import hashlib
from contextlib import ExitStack
import io
import json
from pathlib import Path
import subprocess
import tarfile

import pytest
from pydantic import ValidationError

from debug_ic_fuzzer import (Capture, Compilers, Invalid, Project, Store, commits,
                     differences, normalized, option, prepare_home, replay, within)


def test_text_dedup_binary_and_mtime(tmp_path):
    store = Store(tmp_path / 'store')
    file = tmp_path / 'data.txt'
    file.write_bytes(b'  a\r\nb  \r\n')
    first = store.record(file, True)
    file.write_bytes(b'a\nb')
    second = store.record(file, True)
    assert first['stored'] == second['stored']
    assert first['md5'] == hashlib.md5(b'a\nb').hexdigest()
    assert 'mtime_ns' in first
    archive = tmp_path / 'data.a'
    archive.write_bytes(b'  a\r\nb  \r\n')
    assert store.record(archive)['md5'] == hashlib.md5(archive.read_bytes()).hexdigest()
    assert normalized(b'\0abc\r\n') == (b'\0abc\r\n', False)
    assert len(list((tmp_path / 'store/data.txt').iterdir())) == 1


def test_symlink_escape(tmp_path):
    tracked = tmp_path / 'tracked'
    tracked.mkdir()
    (tracked / 'escape').symlink_to(tmp_path)
    assert not within(tracked / 'escape/out.a', [tracked])
    assert not within(tmp_path / 'tracked-neighbor/out.a', [tracked])


def test_capture_actual_arguments_and_argfile(tmp_path):
    argfile = tmp_path / 'arguments'
    argfile.write_bytes(b'"-g"\n')
    capture = Capture(tmp_path, tmp_path / 'home', tmp_path)
    for line in ['Run "konanc" tool in a separate JVM process', 'Main class = Main',
                 'Arguments = [', '\t-g', '\t-output', '\t"path with spaces/test.kexe"', ']',
                 'Transformed arguments = [', f'\t@{argfile}', ']']:
        capture.line(line + '\n')
    capture.finish()
    assert capture.calls[0]['argv'] == ['-g', '-output', 'path with spaces/test.kexe']
    assert capture.calls[0]['argfiles'][str(argfile)]
    assert option(capture.calls[0]['argv'], '-o', '-output') == 'path with spaces/test.kexe'
    assert option(['-Xic-cache-dir=/tmp/cache'], '-Xic-cache-dir') == '/tmp/cache'


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


def test_comparison_ignores_only_fingerprint_and_mtime(tmp_path):
    store = Store(tmp_path / 'store')
    a, b = {}, {}
    for label, target, fingerprint in [('a', a, 'abc'), ('b', b, 'def')]:
        path = tmp_path / label / 'metadata.properties'
        path.parent.mkdir()
        path.write_text(f'compilerFingerprint={fingerprint}\nother=1\n')
        target['files'] = {'metadata.properties': store.record(path, True) | {'compare': True}}
    assert differences(a, b, store) == []
    path.write_text('compilerFingerprint=def\nother=2')
    b['files']['metadata.properties'] = store.record(path, True) | {'compare': True}
    assert differences(a, b, store) == ['metadata.properties']


@pytest.mark.parametrize('steps,revision_count,exit_code', [
    ([('clean', 'same', 'same'), ('incremental', 'same', 'same'),
      ('incremental', 'same', 'same'), ('clean', 'same', 'same')], 3, 0),
    ([('clean', 'same', 'same'), ('incremental', 'BUILD_FAIL', 'same'),
      ('clean', 'same', 'same'), ('incremental', 'same', 'same'), ('clean', 'same', 'same')], 3, 1),
    ([('clean', 'same', 'same'), ('incremental', 'same', 'changed'), ('clean', 'same', 'same')], 2, 1),
    ([('clean', 'same', 'same'), ('incremental', 'same', 'same'), ('clean', 'changed', 'changed')], 2, 1),
    ([('clean', 'same', 'same'), ('incremental', 'BUILD_FAIL', 'same'), ('clean', 'BUILD_FAIL', 'same')], 2, 1),
    ([('clean', 'same', 'same'), ('incremental', 'same', 'changed'), ('clean', 'same', 'changed')], 2, 1),
    ([('clean', 'BUILD_FAIL', 'same')], 2, 1),
    ([('clean', 'INVALID', 'same')], 2, 2),
    ([('clean', 'same', 'same'), ('incremental', 'same', 'INVALID')], 2, 2),
])
def test_replay_failure_recovery_and_stop(tmp_path, monkeypatch, steps, revision_count, exit_code):
    monkeypatch.setattr('debug_ic_fuzzer.git', lambda *args: '')
    calls, preserved = [], set()
    class Fake:
        def __init__(self, name, column):
            self.name, self.column, self.round, self.checkout = name, column, 0, tmp_path / name
        def build(self, project, step, clean):
            phase, *values = steps[self.round]
            assert clean == (phase == 'clean')
            if clean and self.round:
                assert (self.name, self.round - 1) in preserved  # 清理前必须先留证。
            value = values[self.column]
            calls.append((self.name, clean))
            result = {'status': value if value in {'BUILD_FAIL', 'INVALID'} else 'PASS',
                      'event': (self.name, self.round),
                      'files': {'output': {'compare': True, 'path': 'out.a', 'md5': value}}}
            self.round += 1
            return result
        def preserve(self, result):
            preserved.add(result['event'])
    expected = []
    for phase, baseline, sut in steps:
        expected.append(('baseline', phase == 'clean'))
        if baseline != 'INVALID':
            expected.append(('sut', phase == 'clean'))
    variants = [Fake('baseline', 0), Fake('sut', 1)]
    assert replay(None, variants, list('abc')[:revision_count], tmp_path / 'results', Store(tmp_path / 'store')) == exit_code
    assert calls == expected
    rows = json.loads((tmp_path / 'results/summary.json').read_text())
    assert len([step for row in rows for step in row['steps']]) == len(steps)


def test_collector_includes_klib_and_both_cache_shapes(tmp_path):
    from debug_ic_fuzzer import Compiler, Variant
    checkout, home = tmp_path / 'project', tmp_path / 'home'
    variant = Variant('baseline', checkout, home, Compiler(kotlin_version='2.4.21', kotlin_native_home=home), Store(tmp_path / 'store'))
    ic = checkout / 'app/build/ic'
    klib = checkout / 'lib/build/klib'
    variant.ic_roots.add(ic)
    variant.klib_roots.add(klib)
    files = [ic / 'lib-per-file-cache/file/bin/libfile.a', home / 'klib/cache/lib-cache/bin/liblib.a',
             klib / 'lib/default/ir/bodies.knb', ic / 'digest.json']
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'content')
    records = {v['path']: v for v in variant.collect().values()}
    assert all(records[str(path)]['compare'] for path in files[:3])
    assert not records[str(files[3])]['compare']
    assert records[str(files[3])]['stored']


def test_preserve_rejects_changes_after_collection(tmp_path):
    from debug_ic_fuzzer import Compiler, Variant
    archive = tmp_path / 'output.a'
    archive.write_bytes(b'old')
    store = Store(tmp_path / 'store')
    result = {'files': {'output.a': store.record(archive)}}
    archive.write_bytes(b'new')
    variant = Variant('baseline', tmp_path, tmp_path / 'home', Compiler(kotlin_version='2.4.21', kotlin_native_home='home'), store)
    with pytest.raises(Invalid, match='采集后文件又变化'):
        variant.preserve(result)


def test_capture_preserves_external_argument_file(tmp_path):
    import base64
    args = tmp_path / 'dependencies'
    args.write_bytes(b'  input\r\n')
    capture = Capture(tmp_path, tmp_path, tmp_path)
    for line in ['Arguments = [', f'\t-Xexternal-dependencies={args}', ']']:
        capture.line(line)
    record = capture.calls[0]['argfiles'][str(args)]
    assert base64.b64decode(record['base64']) == args.read_bytes()
    assert record['md5'] == hashlib.md5(b'input').hexdigest()
    assert record['mtime_ns'] == args.stat().st_mtime_ns


@pytest.mark.parametrize('escape', [False, True])
def test_build_collects_rebuilds_and_cache_boundary(tmp_path, monkeypatch, escape):
    from debug_ic_fuzzer import Compiler, Variant
    checkout = tmp_path / 'project'
    cache = checkout / 'app/build/ic'
    archive = tmp_path / 'outside.a' if escape else cache / 'lib-cache/bin/libpart.a'
    untouched = cache / 'lib-cache/bin/libuntouched.a'
    output = checkout / 'app/build/bin/test.kexe'
    invocations = []
    class Process:
        def __init__(self, command, **kwargs):
            step = Path(kwargs['env']['IC_EVAL_STEP'])
            for path in [archive, untouched, output]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'unchanged binary')
            dump = step / 'rebuilt.txt'
            dump.write_text('\n'.join(map(str, [archive, untouched] if not invocations else [archive])) + '\n\n')
            (step / 'link.task.json').write_text(json.dumps({'output': str(output)}))
            args = ['-g', '-Xenable-incremental-compilation', f'-Xic-cache-dir={cache}',
                    '-output', str(output), f'-Xdump-built-caches-to={dump}']
            self.stdout = io.StringIO('Arguments = [\n' + '\n'.join(map(json.dumps, args)) + '\n]\n')
            invocations.append(command)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def wait(self):
            return 0
    monkeypatch.setattr('debug_ic_fuzzer.subprocess.Popen', Process)
    variant = Variant('baseline', checkout, tmp_path / 'home',
                      Compiler(kotlin_version='2.4.21', kotlin_native_home='home'), Store(tmp_path / 'store'))
    project = Project(repository='r', branch='main', start_commit='a', end_commit='b', task=':link',
                      gradle_args=['-Pani.enable.ios=true', '-Pexample=value with spaces'])
    first = variant.build(project, tmp_path / 'first', False)
    assert invocations[0][1:4] == [project.task, *project.gradle_args]
    if escape:
        assert first['status'] == 'INVALID'
        assert f'CACHE_OUT_OF_SCOPE: {archive}' in first['errors']
        assert first['calls'] and first['files'] and first['evidence']
        assert json.loads((tmp_path / 'first/result.json').read_text())['status'] == 'INVALID'
    else:
        second = variant.build(project, tmp_path / 'second', False)
        assert first['status'] == second['status'] == 'PASS'
        assert first['unchanged_rebuilt'] is None
        assert second['unchanged_rebuilt'] == ['project/app/build/ic/lib-cache/bin/libpart.a']
        assert second['retained'] == ['project/app/build/ic/lib-cache/bin/libuntouched.a']
