import hashlib
from contextlib import nullcontext
import io
import json
from pathlib import Path

import pytest

from impl.build import Capture, Variant, option
from impl.config import Compiler, Invalid, Project
from impl.results import Store


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


def test_preserve_rejects_changes_after_collection(tmp_path, monkeypatch):
    monkeypatch.setattr(Variant, "active", lambda self: nullcontext())
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
@pytest.mark.parametrize('explicit_home', [False, True])
def test_build_collects_rebuilds_and_cache_boundary(tmp_path, monkeypatch, escape, explicit_home):
    monkeypatch.setattr(Variant, "active", lambda self: nullcontext())
    checkout = tmp_path / 'project'
    cache = checkout / 'app/build/ic'
    archive = tmp_path / 'outside.a' if escape else cache / 'lib-cache/bin/libpart.a'
    untouched = cache / 'lib-cache/bin/libuntouched.a'
    output = checkout / 'app/build/bin/test.kexe'
    invocations = []
    class Process:
        def __init__(self, command, **kwargs):
            step = Path(kwargs['env']['IC_EVAL_CAPTURE'])
            if len(invocations) == 2:
                assert not cache.exists() and not output.parent.exists()
            for path in [archive, untouched, output]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'unchanged binary')
            dump = step / 'link.rebuilt'
            dump.write_text('\n'.join(map(str, [archive, untouched] if not invocations else [archive])) + '\n\n')
            (step / 'link.task.json').write_text(json.dumps({'output': str(output), 'task': ':link', 'home': str(tmp_path / 'home')}))
            args = ['-g', '-Xenable-incremental-compilation', f'-Xic-cache-dir={cache}',
                    '-output', str(output), f'-Xdump-built-caches-to={dump}']
            self.stdout = io.StringIO('> Task :link\nArguments = [\n' + '\n'.join(map(json.dumps, args)) + '\n]\n')
            invocations.append(command)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def wait(self):
            return 0
    monkeypatch.setattr('impl.build.subprocess.Popen', Process)
    variant = Variant('baseline', checkout, tmp_path / 'home',
                      Compiler(kotlin_version='2.4.21', kotlin_native_home='home' if explicit_home else None), Store(tmp_path / 'store'))
    project = Project(repository='https://example.test/sample.git', branch='main', start_commit='a', end_commit='b', task=':link',
                      gradle_args=['-Pani.enable.ios=true', '-Pexample=value with spaces'])
    first = variant.build(project, tmp_path / 'first', False)
    assert invocations[0][1:4] == [project.task, *project.gradle_args]
    assert any(arg.startswith('-Pkotlin.native.home=') for arg in invocations[0]) == explicit_home
    assert ('-Pkotlin.native.version=2.4.21' in invocations[0]) == (not explicit_home)
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

        # 新一轮复用相同 checkout，只清理上轮登记的 IC 和输出，不动其他文件。
        user_file = checkout / 'user-data.txt'
        user_file.write_text('keep')
        stale = cache / 'stale.a'
        stale.write_bytes(b'old cache')
        reused = Variant('baseline', checkout, tmp_path / 'home', variant.compiler, variant.store)
        third = reused.build(project, tmp_path / 'third', True)
        assert third['status'] == 'PASS' and third['unchanged_rebuilt'] is None
        assert not stale.exists() and user_file.read_text() == 'keep'


def test_identical_build_path_restores_git_and_preserves_each_variant(tmp_path):
    from debug_ic_fuzzer import git
    baseline, sut = tmp_path / 'baseline', tmp_path / 'sut'
    baseline.mkdir()
    git(baseline, 'init', '-b', 'main')
    git(baseline, 'config', 'user.email', 'test@example.com')
    git(baseline, 'config', 'user.name', 'Test')
    (baseline / 'source').write_text('source')
    git(baseline, 'add', 'source')
    git(baseline, 'commit', '-m', 'initial')
    git(baseline, 'worktree', 'add', '--detach', str(sut), 'HEAD')
    store = Store(tmp_path / 'store')
    variants = [Variant(name, tmp_path / name, tmp_path / 'home',
                        Compiler(kotlin_version='2.4.21'), store) for name in ['baseline', 'sut']]
    results = []
    for variant in variants:
        parked = variant.checkout
        with variant.active():
            assert variant.checkout == tmp_path / 'project'
            assert not parked.exists()
            assert git(variant.checkout, 'rev-parse', '--show-toplevel') == str(tmp_path / 'project')
            output = variant.checkout / 'build/output.a'
            output.parent.mkdir()
            output.write_bytes(variant.name.encode() + b'\0')
            results.append({'files': {'output': store.record(output)}})
        assert variant.checkout == parked and parked.exists()
        assert not (tmp_path / 'project').exists()
        assert git(sut, 'rev-parse', '--git-common-dir') == str(baseline / '.git')
    for variant, result in zip(variants, results):
        variant.preserve(result)
        assert (tmp_path / result['files']['output']['stored']).read_bytes() == variant.name.encode() + b'\0'
    with pytest.raises(RuntimeError, match='build failed'):
        with variants[0].active():
            raise RuntimeError('build failed')
    assert baseline.exists() and sut.exists() and not (tmp_path / 'project').exists()
    assert git(sut, 'rev-parse', '--git-common-dir') == str(baseline / '.git')
    (tmp_path / 'project').mkdir()
    with pytest.raises(Invalid, match='尚未恢复'):
        with variants[1].active():
            pass
    assert sut.exists()


@pytest.mark.parametrize('outcome,missing,expected', [
    ('UP-TO-DATE', False, 'PASS'),
    ('UP-TO-DATE', True, 'INVALID'),
    ('FROM-CACHE', False, 'INVALID'),
    ('SKIPPED', False, 'INVALID'),
    ('EXECUTED', False, 'INVALID'),
])
def test_skipped_link_uses_previous_output_without_stale_capture(tmp_path, monkeypatch, outcome, missing, expected):
    monkeypatch.setattr(Variant, 'active', lambda self: nullcontext())
    monkeypatch.setenv('GRADLE_USER_HOME', str(tmp_path / 'user-global'))
    checkout, home = tmp_path / 'project', tmp_path / 'home'
    cache, output = checkout / 'app/build/ic', checkout / 'app/build/bin/app.kexe'
    archive = cache / 'lib.a'
    count = 0
    class Process:
        def __init__(self, command, **kwargs):
            nonlocal count
            assert kwargs['env']['GRADLE_USER_HOME'] == str(tmp_path / 'user-global')
            assert '--no-configuration-cache' not in command and '--no-build-cache' not in command
            capture = Path(kwargs['env']['IC_EVAL_CAPTURE'])
            assert not list(capture.iterdir())
            if count == 0:
                for path in [archive, output]:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b'binary\0')
                dump = capture / 'link.rebuilt'
                dump.write_text(str(archive))
                (capture / 'link.task.json').write_text(json.dumps(dict(task=':link', home=str(home), output=str(output))))
                args = ['-g', '-Xenable-incremental-compilation', f'-Xic-cache-dir={cache}',
                        '-output', str(output), f'-Xdump-built-caches-to={dump}']
                text = '> Task :link\nArguments = [\n' + '\n'.join(map(json.dumps, args)) + '\n]\n'
            else:
                if missing:output.unlink()
                text = '> Task :link' + (f' {outcome}' if outcome != 'EXECUTED' else '') + '\n'
            count += 1
            self.stdout = io.StringIO(text)
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def wait(self):return 0
    monkeypatch.setattr('impl.build.subprocess.Popen', Process)
    variant = Variant('baseline', checkout, home, Compiler(kotlin_version='2.4.21'), Store(tmp_path / 'store'))
    project = Project(repository='https://example.test/sample.git', branch='main', start_commit='a', end_commit='b', task=':link')
    assert variant.build(project, tmp_path / 'first', False)['status'] == 'PASS'
    result = variant.build(project, tmp_path / 'second', False)
    assert result['status'] == expected
    assert result['rebuilt'] == [] and result['calls'] == []
    assert not any(name.endswith(('.rebuilt', '.task.json')) for name in result['evidence'])
    if expected == 'PASS':
        assert result['task_state'] == 'UP-TO-DATE'
        assert result['unchanged_rebuilt'] == [] and result['retained'] == ['project/app/build/ic/lib.a']
