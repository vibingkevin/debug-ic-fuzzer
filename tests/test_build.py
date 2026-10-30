import hashlib
import io
import json
from pathlib import Path

import pytest

from ic_build import Capture, Variant, option
from ic_config import Compiler, Invalid, Project
from ic_results import Store


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


def test_collector_includes_klib_and_both_cache_shapes(tmp_path):
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
    monkeypatch.setattr('ic_build.subprocess.Popen', Process)
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
