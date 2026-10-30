import hashlib

from ic_results import Store, differences, normalized


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
