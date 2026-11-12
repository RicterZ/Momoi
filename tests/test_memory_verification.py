import asyncio
import json
import time
from dataclasses import replace
from unittest.mock import patch

import pytest

from momoi.config.models import ReflectionConfig
from momoi.models import ToolCall
from momoi.runtime import MomoiDaemon
from momoi.runtime.workflows.memory_verification import (
    memory_verification_input, parse_memory_verification,
)
from momoi.storage import Store
from tests.support import config

DAY = 86400
ENABLED = ReflectionConfig(enabled=True)


def remember(store, key, content, *, kind='preference', event_id=None, at=None):
    at = time.time() - 90 * DAY if at is None else at
    event_id = event_id or f'event:{key}'
    with store.transaction():
        store._db.execute(
            "INSERT OR IGNORE INTO events(id,message_id,kind,content,occurred_at,received_at) VALUES (?,?,?,?,?,?)",
            (event_id, event_id, 'message', content, at, at),
        )
    source = {'event_id': event_id, 'quote': content}
    return store.memories.repository.write(
        {'kind': kind, 'key': key, 'content': content, 'activation': 'recall',
         'expires_at': None, 'meta': {'tags': []}}, [], source, [source], now=at)


def episode(store, identifier, created_at, messages, *, topics=(), event_id=None):
    store.create_episode(identifier, episode_id=identifier, topics=list(topics))
    turn_id = f'{identifier}-turn'
    with store.transaction():
        store._db.execute(
            "UPDATE conversation_episodes SET narrative_summary=?, summarized_through_ordinal=1, created_at=? WHERE id=?",
            (f'{identifier} 摘要', created_at, identifier),
        )
        store._db.execute(
            "INSERT INTO turns(id,kind,workflow_kind,source_ids_json,state,started_at,updated_at) "
            "VALUES (?,'owner','owner','[]','completed',?,?)", (turn_id, created_at, created_at),
        )
        store._db.execute(
            "INSERT INTO episode_turns(episode_id,turn_id,ordinal,relation) VALUES (?,?,1,'primary')",
            (identifier, turn_id),
        )
        for index, text in enumerate(messages):
            store._db.execute(
                "INSERT INTO messages(turn_id,role,content,created_at,source_event_ids_json) VALUES (?,'user',?,?,?)",
                (turn_id, text, created_at + index, json.dumps([event_id] if event_id and index == 0 else [])),
            )


def relate(store, newer, older, relation):
    with store.transaction():
        store._db.execute(
            "INSERT INTO episode_relations(source_episode_id,target_episode_id,relation,explanation,"
            "evidence_json,source_summary_ordinal,created_at,updated_at) VALUES (?,?,?,'说明','[]',1,1,1)",
            (newer, older, relation),
        )


def used(store, memory_id, at):
    with store.transaction():
        store._db.execute(
            "INSERT INTO memory_access(memory_id,kind,turn_id,workflow,event_key,created_at) "
            "VALUES (?,'recalled','turn','owner',?,?)", (memory_id, f'k{memory_id}', at),
        )


def verdicts(store, period_end, verdict='still_valid', now=None):
    run = store.claim_due_memory_verification(ENABLED, now)
    source = json.loads(run['input_json'])
    items = []
    for item in source['items']:
        evidence = item['evidence'][0]
        items.append({'memory_id': item['memory']['id'], 'verdict': verdict, 'reason': '材料支持',
                      'citations': [{'source': evidence['id'], 'quote': evidence['text']}]})
    store.begin_turn('verify', 'memory_verification', [period_end])
    store.commit_memory_verification(period_end, 'verify', {'items': items})
    return source


def queue(store, period_end, now=None):
    with store.transaction():
        store._queue_memory_verification(period_end, time.time() if now is None else now)


def test_sampling_prefers_volatile_then_used_stale_then_same_topic(tmp_path):
    store = Store(tmp_path / 'db')
    now = time.time()
    lonely = remember(store, 'lonely', '用户的朋友在杭州工作。', kind='third_party')
    stale = remember(store, 'stale', '用户和妹妹关系很好。', kind='relationship')
    fresh_used = remember(store, 'fresh', '用户和同事常一起午饭。', kind='relationship', at=now - DAY)
    pair_a = remember(store, 'pet.cat', '用户养了一只猫。', kind='third_party')
    pair_b = remember(store, 'pet.dog', '用户没有养宠物。', kind='third_party')
    volatile = [remember(store, f'pref{index}', f'用户喜欢第{index}种茶。') for index in range(3)]
    used(store, stale, now - DAY)
    used(store, fresh_used, now - DAY)
    order = [row['memory']['id'] for row in store.memory_verification_candidates(now=now)]
    assert order[:3] == volatile
    assert order[3] == stale
    assert set(order[4:]) == {pair_a, pair_b}
    assert lonely not in order and fresh_used not in order
    related = {row['memory']['id']: row['related'] for row in store.memory_verification_candidates(now=now)}
    assert [item['id'] for item in related[pair_a]] == [pair_b]
    assert [row['memory']['id'] for row in store.memory_verification_candidates(now=now, limit=2)] == volatile[:2]
    store.close()


def test_cooldown_skips_recent_checks_unless_new_evidence(tmp_path):
    store = Store(tmp_path / 'db')
    now = time.time()
    first = remember(store, 'tea', '用户喜欢绿茶。')
    second = remember(store, 'coffee', '用户喜欢美式咖啡。')
    queue(store, '2026-10-12', now)
    verdicts(store, '2026-10-12', now=now)
    assert store.memory_verification_candidates(now=now + DAY) == []
    store.memories.repository.add_evidence(second, 'event:coffee-again', '还是美式最好', now + DAY)
    assert [row['memory']['id'] for row in store.memory_verification_candidates(now=now + 2 * DAY)] == [second]
    # Activation changes carry no new evidence about the fact itself.
    store.memories.repository.add_evidence(first, 'dashboard:memory:activation:x', '确认升降级', now + DAY)
    later = [row['memory']['id'] for row in store.memory_verification_candidates(now=now + 2 * DAY)]
    assert first not in later
    assert first in [row['memory']['id'] for row in store.memory_verification_candidates(now=now + 31 * DAY)]
    store.close()


def test_results_never_modify_memories_and_only_flags_become_reviews(tmp_path):
    store = Store(tmp_path / 'db')
    old = remember(store, 'drink', '用户喜欢喝冰美式。')
    keep = remember(store, 'food', '用户喜欢吃面。')
    before = {row['id']: row for row in store.memories.repository.inventory()}
    evidence_before = store._db.execute('SELECT COUNT(*) FROM memory_evidence').fetchone()[0]
    queue(store, '2026-10-12')
    source = json.loads(store.claim_due_memory_verification(ENABLED)['input_json'])
    items = {item['memory']['id']: item for item in source['items']}
    store.begin_turn('verify', 'memory_verification', [])
    finding = lambda identifier, verdict: {
        'memory_id': identifier, 'verdict': verdict, 'reason': '判断',
        'citations': [{'source': items[identifier]['evidence'][0]['id'], 'quote': items[identifier]['evidence'][0]['text']}]}
    with pytest.raises(ValueError, match='exactly once'):
        store.commit_memory_verification('2026-10-12', 'verify', {'items': [finding(old, 'possibly_outdated')]})
    bad = finding(old, 'possibly_outdated')
    bad['citations'][0]['quote'] = '用户改喝热茶了'
    with pytest.raises(ValueError, match='verbatim'):
        store.commit_memory_verification('2026-10-12', 'verify', {'items': [bad, finding(keep, 'still_valid')]})
    with pytest.raises(ValueError, match='related memory'):
        store.commit_memory_verification('2026-10-12', 'verify', {'items': [
            {**finding(old, 'conflicts_with'), 'conflicts_with': keep}, finding(keep, 'still_valid')]})
    store.commit_memory_verification('2026-10-12', 'verify', {'items': [
        finding(old, 'possibly_outdated'), finding(keep, 'still_valid')]})
    assert {row['id']: row for row in store.memories.repository.inventory()} == before
    assert store._db.execute('SELECT COUNT(*) FROM memory_evidence').fetchone()[0] == evidence_before
    reviews = store.memory_verification_reviews()['items']
    assert [(row['memory_id'], row['verdict']) for row in reviews] == [(old, 'possibly_outdated')]
    assert store.keep_verified_memory(reviews[0]['id'])['status'] == 'kept'
    assert store.memory_verification_reviews()['items'] == []
    with pytest.raises(LookupError):
        store.keep_verified_memory(reviews[0]['id'])
    assert store.memory_verification_run('2026-10-12')['state'] == 'completed'
    store.close()


def test_review_hides_items_after_memory_changes(tmp_path):
    store = Store(tmp_path / 'db')
    old = remember(store, 'drink', '用户喜欢喝冰美式。')
    queue(store, '2026-10-12')
    verdicts(store, '2026-10-12', 'possibly_outdated')
    assert len(store.memory_verification_reviews()['items']) == 1
    store.update_memory_content(old, '用户现在改喝热茶。')
    assert store.memory_verification_reviews()['items'] == []
    store.close()


def test_context_follows_topic_graph_from_evidence(tmp_path):
    store = Store(tmp_path / 'db')
    base = time.time() - 30 * DAY
    remember(store, 'drink', '我最爱冰美式', event_id='event:drink', at=base)
    episode(store, 'older', base - DAY, ['更早的话题'], topics=['咖啡'])
    episode(store, 'source', base, ['我最爱冰美式'], topics=['咖啡'], event_id='event:drink')
    episode(store, 'follow', base + DAY, ['最近胃不好'])
    episode(store, 'revise', base + 2 * DAY, ['以后不喝冰美式了，改喝热茶'])
    episode(store, 'unrelated', base + 3 * DAY, ['今天去爬山'])
    episode(store, 'topic_old', base - 2 * DAY, ['以前的咖啡'], topics=['咖啡'])
    episode(store, 'topic_new', base + 4 * DAY, ['新开的咖啡店'], topics=['咖啡'])
    relate(store, 'follow', 'source', 'follows_up')
    relate(store, 'revise', 'follow', 'revises')
    relate(store, 'source', 'older', 'follows_up')
    relate(store, 'unrelated', 'source', 'context')
    item = store.memory_verification_candidates()[0]
    context = store.memory_verification_item(item['memory'], item['related'])
    path = [(row['id'], row['relation']) for row in context['later_episodes']]
    assert path == [('episode:source', 'evidence'), ('episode:follow', 'follows_up'),
                    ('episode:revise', 'revises'), ('episode:topic_new', 'same_topic')]
    assert context['evidence'][0]['text'] == '我最爱冰美式'
    messages = context['later_episodes'][2]['messages']
    assert messages[0]['text'] == '以后不喝冰美式了，改喝热茶' and messages[0]['id'].startswith('message:')
    rendered = memory_verification_input({'items': [context]})
    assert '改喝热茶' in rendered
    store.close()


def test_stale_relation_edges_are_ignored(tmp_path):
    store = Store(tmp_path / 'db')
    base = time.time() - 30 * DAY
    episode(store, 'source', base, ['原话'], event_id='event:x')
    episode(store, 'follow', base + DAY, ['后续'])
    relate(store, 'follow', 'source', 'follows_up')
    with store.transaction():
        store._db.execute("UPDATE conversation_episodes SET summarized_through_ordinal=2 WHERE id='follow'")
    assert [row['id'] for row in store.memory_verification_episodes(['event:x'])] == ['episode:source']
    store.close()


def test_weekly_commit_queues_verification_and_workflow_completes(tmp_path):
    from tests.test_weekly_reflection_storage import seed, stamp
    daemon = MomoiDaemon(replace(config(str(tmp_path)), timezone='Asia/Shanghai', reflection=ENABLED))
    store = daemon.store
    remember(store, 'drink', '用户喜欢喝冰美式。')
    seed(store, '2026-10-08')
    now = stamp('2026-10-12T05:00:00')
    store.claim_due_weekly_reflection(ENABLED, now)
    assert store.claim_due_memory_verification(ENABLED, now) is None
    store.begin_turn('weekly', 'weekly_reflection', [])
    store.commit_weekly_reflection('2026-10-12', 'weekly', {'summary': '无', 'findings': []})
    assert store.claim_due_memory_verification(ReflectionConfig(), now) is None
    assert store.next_memory_verification_due_at(ENABLED) is not None
    run = store.claim_due_memory_verification(ENABLED, now)
    assert run['state'] == 'running'

    async def fail(*args, **kwargs):
        raise RuntimeError('provider failed')

    daemon._run_agent_workflow = fail
    asyncio.run(daemon._complete_memory_verification_turn('2026-10-12', asyncio.Event()))
    assert store.memory_verification_run('2026-10-12')['state'] == 'pending'
    retry_at = store.memory_verification_run('2026-10-12')['retry_at']
    assert store.claim_due_memory_verification(ENABLED, retry_at - 1) is None
    store.claim_due_memory_verification(ENABLED, retry_at)

    async def finish(system, messages, tools, *, turn_id, workflow):
        assert [tool['name'] for tool in tools] == ['memory_verification_finish']
        assert '冰美式' in messages[0]['content']
        source = json.loads(store.memory_verification_run('2026-10-12')['input_json'])
        evidence = source['items'][0]['evidence'][0]
        invalid = await workflow.execute_tool(ToolCall('bad', 'memory_verification_finish', {'items': []}))
        assert invalid['ok'] is False
        result = await workflow.execute_tool(ToolCall('done', 'memory_verification_finish', {'items': [{
            'memory_id': source['items'][0]['memory']['id'], 'verdict': 'still_valid', 'reason': '仍成立',
            'citations': [{'source': evidence['id'], 'quote': evidence['text']}]}]}))
        assert result['ok']
        return workflow.completion_result()

    daemon._run_agent_workflow = finish
    asyncio.run(daemon._complete_memory_verification_turn('2026-10-12', asyncio.Event()))
    assert store.memory_verification_run('2026-10-12')['state'] == 'completed'
    assert store.memory_verification_reviews()['items'] == []
    store.close()


def test_empty_sample_completes_without_model(tmp_path):
    daemon = MomoiDaemon(replace(config(str(tmp_path)), reflection=ENABLED))
    queue(daemon.store, '2026-10-12')
    daemon.store.claim_due_memory_verification(ENABLED)

    async def unexpected(*args, **kwargs):
        raise AssertionError('model should not run')

    daemon._run_agent_workflow = unexpected
    asyncio.run(daemon._complete_memory_verification_turn('2026-10-12', asyncio.Event()))
    assert daemon.store.memory_verification_run('2026-10-12')['state'] == 'completed'
    daemon.store.close()


def test_scheduler_enqueues_and_owner_stop_releases(tmp_path):
    daemon = MomoiDaemon(replace(config(str(tmp_path)), reflection=ENABLED))

    async def run():
        stop = asyncio.Event()

        def claim(_config):
            stop.set()
            return {'period_end': '2026-10-12'}
        with patch.object(daemon.store, 'claim_due_reflection', return_value=None), \
             patch.object(daemon.store, 'claim_due_weekly_reflection', return_value=None), \
             patch.object(daemon.store, 'claim_due_memory_verification', side_effect=claim):
            await daemon._scheduler_worker(stop)
        job = daemon.autonomous.get_nowait()
        assert job.kind == 'memory_verification' and job.id == '2026-10-12'
        assert daemon._release_autonomous_claim(job) == {'period_end': '2026-10-12'}
    asyncio.run(run())
    daemon.store.close()


def test_restart_resets_running_verification(tmp_path):
    path = tmp_path / 'db'
    store = Store(path)
    queue(store, '2026-10-12')
    store.claim_due_memory_verification(ENABLED)
    store.close()
    store = Store(path)
    assert store.memory_verification_run('2026-10-12')['state'] == 'pending'
    store.close()


def test_parse_rejects_blank_reason():
    item = {'memory_id': 1, 'verdict': 'still_valid', 'reason': ' ', 'citations': []}
    assert parse_memory_verification({'items': [item]})[1]
    assert parse_memory_verification({'items': [{**item, 'verdict': 'invented', 'reason': '理由'}]})[1]
    assert parse_memory_verification({'items': [{**item, 'reason': '理由'}]})[1] is None
