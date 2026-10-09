"""Historical replay must not count future or superseded versions as targets."""
import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    'candidate_calibration',
    Path(__file__).resolve().parents[1] / 'scripts/calibrate_memory_candidates.py',
)
calibration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(calibration)


def memory(identifier, created_at, superseded_by=None):
    return {'id': identifier, 'created_at': created_at, 'updated_at': created_at,
            'superseded_by': superseded_by, 'scope_key': '', 'expires_at': None,
            'meta_json': '{}', 'kind': 'preference', 'key': 'coffee', 'content': 'current body'}


def batch(sequence, created_at, target, snapshot=None):
    return {'sequence': sequence, 'created_at': created_at,
            'operations_json': json.dumps([{'id': 'op', 'content': 'coffee', 'type': 'replace'}]),
            'result_json': json.dumps([{'operation_ids': ['op'], 'action': 'write', 'target_ids': [target]}]),
            'context_json': json.dumps([snapshot] if snapshot else [])}


def test_history_uses_only_versions_existing_at_submission_and_keeps_snapshot_text():
    data = {'memories': [memory(1, 10, 2), memory(2, 30), memory(3, 50)],
            'memory_tombstones': [],
            'memory_operation_batches': [
                batch(1, 20, 1, {'id': 1, 'content': 'actual captured body'}),
                batch(2, 40, 2), batch(3, 40, 1),
            ]}
    cases = calibration.samples(data)
    assert len(cases) == 2
    assert [row['id'] for row in cases[0]['rows']] == [1]
    assert cases[0]['rows'][0]['content'] == 'actual captured body'
    assert [row['id'] for row in cases[1]['rows']] == [2]
    assert data['memories'][0]['content'] == 'current body'


def test_future_deletion_does_not_hide_valid_historical_target():
    tombstone = {'kind': 'preference', 'key': 'coffee', 'scope_key': '',
                 'created_at': 30, 'source_event_id': 'forget', 'evidence_quote': 'delete'}
    data = {'memories': [memory(1, 10)], 'memory_tombstones': [tombstone],
            'memory_operation_batches': [batch(1, 20, 1), batch(2, 40, 1)]}
    before, after = calibration.samples(data)
    assert before['rows'][0]['forgotten_at'] is None
    assert after['rows'][0]['forgotten_at'] == 30
    assert after['rows'][0]['forgotten_event_id'] == 'forget'
