import json

import pytest

from debug_ic_fuzzer import replay
from impl.results import Store


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
