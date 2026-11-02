from pathlib import Path
import shutil
import subprocess

import pytest

from impl.collection import collect
from impl.comparison import compare


@pytest.fixture
def products(tmp_path):
    source = tmp_path / 'source.c'
    results = {}
    for name in ('baseline', 'sut'):
        output = tmp_path / name / 'build/libdemo.so'
        output.parent.mkdir(parents=True)
        source.write_text('int value(void) { return 1; }\n')
        subprocess.run(['cc', '-g', '-shared', '-fPIC', str(source), '-o', str(output)], check=True)
        (output.parent / 'libdemo_api.h').write_text('int value(void);\n')
        results[name] = dict(variant=name, cwd=str(tmp_path / 'project'),
                             link={'output': str(tmp_path / 'project/build/libdemo.so')})
    # Start with byte-identical products, independent of the host linker's defaults.
    shutil.copyfile(tmp_path / 'baseline/build/libdemo.so', tmp_path / 'sut/build/libdemo.so')
    return results


def test_equal_products_keep_hashes_without_contents(tmp_path, products):
    assert compare(products, tmp_path) == dict(strict_equal=True, production_equal=True)
    assert not (tmp_path / "store").exists()
    for result in products.values():
        assert len(result["artifacts"]) == 2
        binary = next(f for f in result["artifacts"] if f["path"].endswith(".so"))
        assert len(binary["md5"]) == len(binary["production_md5"]) == 32
        assert binary["md5"] != binary["production_md5"]


@pytest.mark.parametrize('change', ['debug', 'build-id', 'code', 'header', 'missing'])
def test_product_differences(tmp_path, products, change):
    output = tmp_path / 'sut/build/libdemo.so'
    if change == 'debug':
        data = tmp_path / 'debug-data'
        data.write_bytes(b'different debug information')
        subprocess.run(['objcopy', f'--add-section=.debug_test={data}', str(output)], check=True)
    elif change == 'build-id':
        data = tmp_path / 'note'
        subprocess.run(['objcopy', f'--dump-section=.note.gnu.build-id={data}', str(output)], check=True)
        content = bytearray(data.read_bytes())
        content[-1] ^= 1
        data.write_bytes(content)
        subprocess.run(['objcopy', f'--update-section=.note.gnu.build-id={data}', str(output)], check=True)
    elif change == 'code':
        source = tmp_path / 'source.c'
        source.write_text('int value(void) { return 2; }\n')
        subprocess.run(['cc', '-g', '-shared', '-fPIC', str(source), '-o', str(output)], check=True)
    elif change == 'header':
        (output.parent / 'libdemo_api.h').write_text('long value(void);\n')
    else:
        (output.parent / 'extra.h').write_text('int extra(void);\n')
    result = compare(products, tmp_path)
    assert result['strict_equal'] is False
    assert result['production_equal'] == (change in ('debug', 'build-id'))
    for record in products.values():
        for file in record['artifacts']:
            name = Path(file['path']).name
            saved = tmp_path / 'store' / name / f"{name}-{file['md5']}"
            original = tmp_path / record['variant'] / Path(file['path']).relative_to('project')
            assert saved.read_bytes() == original.read_bytes()
            assert set(file) == {'path', 'md5', 'mtime_ns', 'production_md5'}


def test_header_whitespace_is_nonblocking_but_originals_are_saved(tmp_path, products):
    (tmp_path / 'sut/build/libdemo_api.h').write_bytes(b' \r\nint value(void);\r\n')
    result = compare(products, tmp_path)
    assert not result['strict_equal'] and result['production_equal']
    assert len(list((tmp_path / 'store/libdemo_api.h').iterdir())) == 2


def test_cache_snapshot_normalization_dedup_and_no_binary_contents(tmp_path):
    root = tmp_path / 'baseline/build/ic'
    root.mkdir(parents=True)
    metadata = root / 'metadata.properties'
    metadata.write_bytes(b' hash=abc\r\ncompilerFingerprint=old\r\n')
    archive = root / 'code.a'
    archive.write_bytes(b'archive')
    (root / 'inline_bodies').write_bytes(b'\x00\x01')
    native = tmp_path / 'native/klib/cache'
    native.mkdir(parents=True)
    (native / 'bitcode_deps').write_text('stdlib\n')
    record = dict(variant='baseline', cwd=str(tmp_path / 'project'), native_home=str(tmp_path / 'native'),
                  link={}, task_state='EXECUTED', exit_code=0, log='gradle.log')
    (tmp_path / 'gradle.log').write_text(f'\t-Xic-cache-dir={tmp_path}/project/build/ic\n')
    collect(record, tmp_path)
    first = next(f for f in record['cache_files'] if f['path'].endswith('metadata.properties'))
    metadata.write_text('hash=abc\ncompilerFingerprint=new\n')
    collect(record, tmp_path)
    second = next(f for f in record['cache_files'] if f['path'].endswith('metadata.properties'))
    assert first['md5'] == second['md5']
    assert len(list((tmp_path / 'store/metadata.properties').iterdir())) == 1
    assert not (tmp_path / 'store/code.a').exists()
    assert not (tmp_path / 'store/inline_bodies').exists()
    assert len(record['cache_files']) == 4
    assert all(f['path'].startswith(('project/', 'native-home/')) for f in record['cache_files'])
    # UP-TO-DATE must still inspect current files using the previous link metadata.
    record['task_state'] = 'UP-TO-DATE'
    (tmp_path / 'gradle.log').write_text('')
    collect(record, tmp_path)
    assert len(record['cache_files']) == 4


@pytest.mark.parametrize('escape', ['ic', 'auto', 'archive', 'symlink'])
def test_cache_boundary_errors(tmp_path, escape):
    (tmp_path / 'baseline/build/ic').mkdir(parents=True)
    record = dict(variant='baseline', cwd=str(tmp_path / 'project'), native_home=str(tmp_path / 'native'),
                  link={}, task_state='EXECUTED', exit_code=0, log='gradle.log')
    log = f'-Xic-cache-dir={tmp_path}/project/build/ic\n'
    if escape == 'ic':
        log = f'-Xic-cache-dir={tmp_path}/outside\n'
    elif escape == 'auto':
        log += f'-Xauto-cache-dir={tmp_path}/outside\n'
    elif escape == 'archive':
        record['link']['built_caches'] = str(tmp_path / 'project/build/built.txt')
        (tmp_path / 'baseline/build/built.txt').write_text(str(tmp_path / 'outside.a'))
    else:
        (tmp_path / 'outside.a').touch()
        (tmp_path / 'baseline/build/ic/escape.a').symlink_to(tmp_path / 'outside.a')
    (tmp_path / 'gradle.log').write_text(log)
    with pytest.raises(ValueError, match='outside|symlink'):
        collect(record, tmp_path)


def test_strip_error_keeps_both_originals(tmp_path, products, monkeypatch):
    (tmp_path / 'sut/build/libdemo_api.h').write_text('long value(void);\n')
    def fail(*_):
        raise OSError('strip unavailable')
    monkeypatch.setattr('impl.comparison.production_digest', fail)
    with pytest.raises(OSError, match='strip unavailable'):
        compare(products, tmp_path)
    assert all(len(result['artifacts']) == 2 for result in products.values())
    for result in products.values():
        for f in result['artifacts']:
            name = Path(f['path']).name
            assert (tmp_path / 'store' / name / f"{name}-{f['md5']}").is_file()
