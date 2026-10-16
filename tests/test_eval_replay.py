import json

import pytest

from evals.replay import SHAPES, recovery_case


@pytest.mark.parametrize('fault', json.loads(SHAPES.read_text())['faults'])
def test_real_storage_recovery_faults(tmp_path, fault):
    result = recovery_case(fault, tmp_path)
    assert result['status'] == 'pass', result
    assert result['checks']


def test_production_shapes_have_no_raw_records():
    shapes = json.loads(SHAPES.read_text())
    assert set(shapes) == {'provenance', 'observed', 'limitations', 'memory_cases', 'faults'}
    assert set(shapes['observed']['memory_operation_types']) == {'add', 'replace', 'forget'}
