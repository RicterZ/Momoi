import pytest

from momoi.memory import PlanningContext
from tests.support import seed_memory
from tests.test_memory_operations import store, event, submit, write


def test_manual_content_and_triggers_create_versions_and_authenticated_evidence(store):
    source = event(store, text='我喜欢甜咖啡')
    first = seed_memory(store, source, content=source.text, key='drink', activation='recall')
    original = dict(store._db.execute('SELECT * FROM memories WHERE id=?', (first,)).fetchone())
    second = store.update_memory_content(first, '我喜欢无糖咖啡', triggers=['咖啡'])
    assert second['id'] != first
    old = store._db.execute('SELECT * FROM memories WHERE id=?', (first,)).fetchone()
    assert old['content'] == original['content'] and old['meta_json'] == original['meta_json']
    assert old['superseded_by'] == second['id']
    assert store.update_memory_content(first, '迟到的修改') is None
    assert store.update_memory_content(second['id'], second['content'], triggers=['咖啡'])['id'] == second['id']
    third = store.update_memory_content(second['id'], triggers=[])
    assert third['id'] != second['id'] and third['meta']['triggers'] == []
    assert store.memories.snapshots([second['id']]) == {}
    assert store._db.execute('SELECT superseded_by FROM memories WHERE id=?', (second['id'],)).fetchone()[0] == third['id']
    evidence = store.memory_evidence_for_memories([third['id']])
    assert len(evidence) == 3
    assert any(row['content'] == source.text for row in evidence)
    manual = [row for row in evidence if row['event_id'].startswith('dashboard:memory:')]
    assert len(manual) == 2 and '无糖咖啡' in manual[0]['content']
    records = store.memory_operation_evidence_records({row['event_id']: row['content'] for row in evidence})
    assert len(records) == 3 and all(row['received_at'] > 0 for row in records)
    assert store.memory_operation_evidence_records({manual[0]['event_id']: '伪造手动修改'}) == []
    # Editing the dashboard must not create a new chat message to dispatch.
    assert store._db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 1


def test_manual_edit_invalidates_in_flight_plan(store):
    source = event(store)
    first = seed_memory(store, source, content='旧事实', key='drink', activation='recall')
    context = PlanningContext([{'id': 'op', 'type': 'replace', 'target_id': first,
        'content': source.text, 'event_id': source.event_id, 'evidence': source.text}],
        {source.event_id: source.text}, store.memories.snapshots([first]))
    planned = store.memories.writing.review(context, {'decisions': [write(source, targets=[first])]})
    new = store.update_memory_content(first, '手动纠正')
    with pytest.raises(ValueError, match='snapshot_changed'):
        store.memories.apply(planned, operation_id='stale')
    assert store.memories.snapshots([new['id']])[new['id']]['content'] == '手动纠正'


def test_host_accepts_persisted_manual_evidence_without_trusting_forged_sources(store):
    old_source = event(store, name='old', text='我喝茶')
    first = seed_memory(store, old_source, content='我喝茶', key='drink', activation='recall')
    second = store.update_memory_content(first, '我喜欢咖啡')
    source = event(store, name='new', text='我现在喜欢无糖咖啡')
    submit(store, source)
    batch = store.claim_memory_operation('source')
    evidence = {source.event_id: source.text, **{row['event_id']: row['content']
        for row in store.memory_evidence_for_memories([second['id']])}}
    context = PlanningContext(batch['operations'], evidence, store.memories.snapshots([second['id']]))
    planned = store.memories.writing.review(context, {'decisions': [write(source, targets=[second['id']], content="我喜欢无糖咖啡")]})
    store.apply_memory_operation(batch, planned)
    assert store.memories.repository.active('preference', 'drink')['content'] == '我喜欢无糖咖啡'
    assert store._db.execute("SELECT state FROM memory_operation_batches WHERE id='source'").fetchone()[0] == 'completed'
