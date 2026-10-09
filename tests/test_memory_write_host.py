import asyncio

import pytest

from momoi.memory import PlanningContext
from momoi.models import ToolCall
from tests.test_memory_operations import store, daemon, event, submit, write, response


def reviewed(store, batch):
    context = PlanningContext(batch['operations'], {item['event_id']: item['text'] for item in batch['events']}, {})
    return store.memories.writing.review(context, {'decisions': [write_event(batch)]})


def write_event(batch):
    class Source:
        event_id = batch['events'][0]['event_id']
        text = batch['events'][0]['text']
    return write(Source())


def test_host_journal_failure_rolls_back_memory_receipt_and_batch(store, monkeypatch):
    source = event(store)
    submit(store, source)
    batch = store.claim_memory_operation('source')
    plan = reviewed(store, batch)
    def fail(*args, **kwargs):
        assert store._db.execute('SELECT COUNT(*) FROM memory_commits').fetchone()[0] == 1
        assert store._db.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 1
        raise RuntimeError('journal failed')
    monkeypatch.setattr(store, '_append_turn_journal', fail)
    with pytest.raises(RuntimeError, match='journal failed'):
        store.apply_memory_operation(batch, plan.decisions, plan.snapshots, plan=plan)
    for table in ('memories', 'memory_commits', 'memory_evidence'):
        assert store._db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    assert store._db.execute("SELECT state FROM memory_operation_batches WHERE id='source'").fetchone()[0] == 'running'
    assert store._db.execute('SELECT state FROM turns WHERE id=?', (batch['turn_id'],)).fetchone()[0] == 'running'


def test_host_rechecks_full_authenticated_evidence_before_apply(store):
    source = event(store)
    submit(store, source)
    batch = store.claim_memory_operation('source')
    plan = reviewed(store, batch)
    with store.transaction():
        # Quote still matches, but surrounding evidence changed after planning.
        store._db.execute("UPDATE events SET content=content || '，刚才是举例' WHERE id=?", (source.event_id,))
    with pytest.raises(ValueError, match='memory_operation_evidence_changed'):
        store.apply_memory_operation(batch, plan.decisions, plan.snapshots, plan=plan)
    assert store._db.execute('SELECT COUNT(*) FROM memory_commits').fetchone()[0] == 0
    assert store.memory.inventory() == []


def test_runtime_model_completion_only_plans_then_host_applies(daemon, monkeypatch):
    source = event(daemon.store)
    submit(daemon.store, source)
    run = daemon._run_agent_workflow
    seen = []
    async def model(*args, **kwargs):
        return response(ToolCall('finish', 'memory_operation_finish', {'decisions': [write(source)]}))
    async def check_after_model(*args, **kwargs):
        result = await run(*args, **kwargs)
        assert daemon.store.memory.inventory() == []
        assert daemon.store._db.execute('SELECT COUNT(*) FROM memory_commits').fetchone()[0] == 0
        seen.append(result)
        return result
    daemon.provider = type('Provider', (), {'complete': staticmethod(model)})()
    monkeypatch.setattr(daemon, '_run_agent_workflow', check_after_model)
    asyncio.run(daemon._complete_memory_operation_turn('source', asyncio.Event()))
    assert len(seen) == 1 and seen[0]['state'] == 'planned'
    assert daemon.store.active_memory('preference', 'drink')
    assert daemon.store._db.execute('SELECT operation_id FROM memory_commits').fetchone()[0] == 'owner-memory:source'
    assert daemon.store._db.execute("SELECT state FROM memory_operation_batches WHERE id='source'").fetchone()[0] == 'completed'
