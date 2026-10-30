import hashlib
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
    temporary = []
    try:
        with pytest.raises(tarfile.FilterError):
            prepare_home(path, temporary)
    finally:
        for temp in temporary:
            temp.cleanup()


def test_configs_reject_unexpected_and_no_project_compiler_dependency():
    with pytest.raises(ValidationError):
        Project(repository='r', branch='main', start_commit='a', end_commit='b', task=':t', output='x')
    c = Compilers.model_validate({'compilers': {'baseline': {'kotlin_version': '2.4.21', 'kotlin_native_home': 'b'},
                                               'sut': {'kotlin_version': '2.4.21', 'kotlin_native_home': 's'}}})
    assert c.compilers['sut'].kotlin_native_home == Path('s')


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
    p = Project(repository='r', branch='main', start_commit=revisions[0], end_commit=revisions[2], task=':t')
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


@pytest.mark.parametrize('failure', ['DIFF', 'BUILD_FAIL'])
def test_failure_cleans_both_and_keeps_failure(tmp_path, monkeypatch, failure):
    monkeypatch.setattr('debug_ic_fuzzer.git', lambda *args: '')
    calls = []
    class Fake:
        def __init__(self, name):
            self.name, self.checkout = name, tmp_path / name
        def build(self, project, step, clean):
            calls.append((self.name, clean))
            step.mkdir(parents=True)
            value = 'different' if failure == 'DIFF' and self.name == 'sut' and not clean else 'same'
            return {'status': 'BUILD_FAIL' if failure == 'BUILD_FAIL' and self.name == 'baseline' and not clean else 'PASS',
                    'files': {'output': {'compare': True, 'path': 'out.a', 'md5': value}}}
        def preserve(self, result):
            pass
    assert not replay(None, [Fake('baseline'), Fake('sut')], ['a', 'b'], tmp_path / 'results', Store(tmp_path / 'store'))
    assert calls == [('baseline', True), ('sut', True), ('baseline', False), ('sut', False), ('baseline', True), ('sut', True)]
    rows = json.loads((tmp_path / 'results/summary.json').read_text())
    assert rows[-1]['steps'][0]['status'] == failure
    assert rows[-1]['steps'][1]['status'] == 'PASS'


def test_invalid_stops_before_other_variant(tmp_path, monkeypatch):
    monkeypatch.setattr('debug_ic_fuzzer.git', lambda *args: '')
    calls = []
    class Fake:
        checkout = tmp_path
        name = 'baseline'
        def build(self, *args):
            calls.append(1)
            return {'status': 'INVALID', 'files': {}}
        def preserve(self, result):
            pass
    assert not replay(None, [Fake(), Fake()], ['a'], tmp_path / 'results', Store(tmp_path / 'store'))
    assert calls == [1]


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
