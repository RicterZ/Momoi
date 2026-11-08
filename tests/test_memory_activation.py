import asyncio
import time

from aiohttp.test_utils import TestClient, TestServer
import pytest

from momoi.config.models import ConfigError, ReflectionConfig
from momoi.config.loading import load_config
from momoi.dashboard.app import create_dashboard_app
from momoi.dashboard.settings import DashboardSettings
from momoi.models import AgentReply, TurnDraft
from tests.test_memory_catalog import seed
from tests.test_memory_writing import decision, context

DAY = 86400


def seed_volatile(store):
    identifier = seed(store.memories, 'volatile', [], activation='always')
    # Imported legacy always states can predate the current kind restrictions.
    with store.transaction():
        store._db.execute("UPDATE memories SET kind='cross_event_state' WHERE id=?", (identifier,))
    return identifier


def reviewed(store, demote_ids=()):
    command = decision(key='new')
    command['memory']['activation'] = 'always'
    command['demote_ids'] = list(demote_ids)
    return store.memories.writing.review(context(store.memories, demote_ids), {'decisions': [command]})


def test_budget_rejects_add_replacement_and_manual_edit_atomically(store):
    old = seed(store.memories, 'old', [], activation='always')
    repository = store.memories.repository
    repository.always_token_budget = store.always_memory_tokens()
    before = store.memories.snapshots([old])
    for change in [lambda: seed(store.memories, 'new', [], activation='always'),
                   lambda: store.update_memory_content(old, '很长的新内容' * 100),
                   lambda: repository.update_meta(before[old], {'tags': ['health']})]:
        with pytest.raises(ValueError, match='token budget exceeded'):
            change()
        assert store.memories.snapshots([old]) == before
        assert len(repository.inventory()) == 1
    with pytest.raises(ValueError, match='token budget exceeded'):
        reviewed(store)
    assert store._db.execute('SELECT COUNT(*) FROM memory_commits').fetchone()[0] == 0


def test_reviewer_can_demote_in_same_atomic_batch_and_retry(store):
    old = seed(store.memories, 'old', [], activation='always')
    repository = store.memories.repository
    repository.always_token_budget = 90
    planned = reviewed(store, [old])
    result = store.memories.apply(planned, operation_id='budget-review')
    assert store.memories.apply(planned, operation_id='budget-review') == result
    assert [row['key'] for row in repository.rows('always')] == ['new']
    assert [row['key'] for row in repository.rows('recall')] == ['old']
    assert store.always_memory_tokens() <= 90
    assert store._db.execute('SELECT 1 FROM memory_evidence WHERE memory_id=? AND source_event_id=?',
                             (repository.rows('recall')[0]['id'], 'e')).fetchone()


def test_apply_rechecks_budget_after_concurrent_write_and_rolls_back_demotions(store):
    old = seed(store.memories, 'old', [], activation='always')
    repository = store.memories.repository
    repository.always_token_budget = 100
    planned = reviewed(store, [old])
    seed(store.memories, 'racing', [], activation='always')
    repository.always_token_budget = store.always_memory_tokens()
    before = repository.inventory()
    with pytest.raises(ValueError, match='token budget exceeded'):
        store.memories.apply(planned, operation_id='racing')
    assert repository.inventory() == before
    assert not store._db.execute("SELECT 1 FROM memory_commits WHERE operation_id='racing'").fetchone()


@pytest.mark.parametrize('bad', [[True], [999], [1, 1]])
def test_invalid_demotions_rejected(store, bad):
    seed(store.memories, 'old', [], activation='always')
    with pytest.raises(ValueError):
        reviewed(store, bad)


def add_usage(store, identifier, now, *, kinds=None, topics=None):
    kinds = kinds or ['recalled', 'read', 'read', 'recalled', 'read']
    topics = topics or ['a', 'b', 'c', 'a', 'b']
    for topic in set(topics):
        if not store.episode(topic):
            store.create_episode(topic, episode_id=topic)
    for index, (kind, topic) in enumerate(zip(kinds, topics)):
        turn = f'{identifier}-{index}'
        store.begin_turn(turn, 'owner', [])
        with store.transaction():
            store._db.execute("INSERT INTO episode_turns(episode_id,turn_id,ordinal,relation) VALUES(?,?,?,'primary')",
                              (topic, turn, identifier * 10 + index + 1))
            store._commit_memory_access(turn, [{'memory_id': identifier, 'kind': kind, 'event_key': str(index), 'created_at': now-index}])


def test_promotion_threshold_topics_time_window_and_lineage(store):
    now = time.time()
    eligible = seed(store.memories, 'eligible', [])
    two = seed(store.memories, 'two', [])
    old = seed(store.memories, 'old', [])
    browse = seed(store.memories, 'browse', [])
    scoped = seed(store.memories, 'scoped', [], scope='webhook')
    add_usage(store, eligible, now)
    add_usage(store, two, now, topics=['a', 'b', 'a', 'b', 'a'])
    add_usage(store, old, now-31*DAY)
    add_usage(store, browse, now, kinds=['browsed']*5)
    add_usage(store, scoped, now)
    replacement = store.update_memory_content(eligible, '新版')['id']
    before = store.memories.repository.inventory()
    candidates = store.generate_memory_activation_suggestions(now=now)
    assert [(item['memory_id'], item['target']) for item in candidates] == [(replacement, 'always')]
    assert candidates[0]['hits'] == 5 and candidates[0]['topics'] == 3
    store.generate_memory_activation_suggestions(now=now)
    assert len(store.memory_activation_suggestions()['items']) == 1
    assert store.memories.repository.inventory() == before


def test_demotion_ignores_injection_requires_full_observation_and_preserves_usage(store):
    now = time.time()
    volatile = seed_volatile(store)
    unused = seed(store.memories, 'unused', [], activation='always')
    used = seed(store.memories, 'used', [], activation='always')
    with store.transaction():
        store._db.execute("UPDATE memories SET kind='relationship' WHERE id IN (?,?)", (unused, used))
    assert [item['memory_id'] for item in store.memory_activation_candidates(now=now)] == [volatile]
    with store.transaction():
        store._db.execute('UPDATE memory_usage_observation SET started_at=?', (now-61*DAY,))
        store._commit_memory_access('usage', [
            {'memory_id': used, 'kind': 'used', 'event_key': 'reply', 'created_at': now-1},
            {'memory_id': unused, 'kind': 'always', 'event_key': 'prefix', 'created_at': now-1},
        ])
    assert {item['memory_id'] for item in store.memory_activation_candidates(now=now)} == {volatile, unused}
    replaced = store.update_memory_content(used, '新内容')['id']
    assert replaced not in {item['memory_id'] for item in store.memory_activation_candidates(now=now)}


def test_approve_reject_stale_suggestions_and_budget_failure(store):
    old = seed(store.memories, 'old', [], activation='always')
    target = seed(store.memories, 'target', [])
    add_usage(store, target, time.time())
    store.generate_memory_activation_suggestions()
    items = store.memory_activation_suggestions()['items']
    promotion = next(item for item in items if item['target'] == 'always')
    store.memories.repository.always_token_budget = store.always_memory_tokens() + 3
    before = store.memories.repository.inventory()
    with pytest.raises(ValueError, match='token budget exceeded'):
        store.review_memory_activation_suggestion(promotion['id'], approve=True)
    assert store.memories.repository.inventory() == before
    result = store.review_memory_activation_suggestion(promotion['id'], approve=True, demote_ids=[old])
    assert result['status'] == 'approved'
    assert [row['key'] for row in store.memories.repository.rows('always')] == ['target']
    assert not store.memory_activation_suggestions()['items']
    with pytest.raises(LookupError):
        store.review_memory_activation_suggestion(promotion['id'], approve=True)
    store.generate_memory_activation_suggestions(now=time.time() + 61*DAY)
    item = store.memory_activation_suggestions()['items'][0]
    store.update_memory_content(item['memory_id'], '已变化')
    with pytest.raises(ValueError, match='snapshot_changed'):
        store.review_memory_activation_suggestion(item['id'], approve=True)


def test_weekly_empty_review_still_generates_suggestions(store):
    seed_volatile(store)
    record = store.claim_due_weekly_reflection(ReflectionConfig(enabled=True))
    store.begin_turn('weekly', 'weekly_reflection', [])
    before = store.memories.repository.inventory()
    store.commit_weekly_reflection(record['period_end'], 'weekly', {'summary': '没有观察', 'findings': []})
    assert len(store.memory_activation_suggestions()['items']) == 1
    assert store.memories.repository.inventory() == before


def test_prefix_order_stays_stable_when_version_id_changes(store):
    z = seed(store.memories, 'z', [], activation='always')
    a = seed(store.memories, 'a', [], activation='always')
    assert [row['key'] for row in store.memories.repository.rows('always')] == ['a', 'z']
    store.update_memory_content(a, '改写')
    text = store.always_memory_context()
    assert text.index('key="a"') < text.index('key="z"')


def test_activation_api_requires_auth_and_explicit_confirmation(store):
    async def run():
        seed_volatile(store)
        store.generate_memory_activation_suggestions()
        from momoi.dashboard.auth import issue_dashboard_jwt
        headers = {'Authorization': 'Bearer ' + issue_dashboard_jwt('secret')}
        app = create_dashboard_app(store, token='secret', settings=DashboardSettings(()))
        async with TestClient(TestServer(app)) as client:
            assert (await client.get('/api/memory-activation-suggestions')).status == 401
            response = await client.get('/api/memory-activation-suggestions', headers=headers)
            item = (await response.json())['items'][0]
            url = f"/api/memory-activation-suggestions/{item['id']}/review"
            assert (await client.post(url, json={}, headers=headers)).status == 400
            response = await client.post(url, json={'approve': False}, headers=headers)
            assert response.status == 200
            assert (await response.json())['status'] == 'rejected'
        assert len(store.memories.repository.rows('always')) == 1
        store.generate_memory_activation_suggestions()
        assert not store.memory_activation_suggestions()['items']
    asyncio.run(run())


def test_always_evaluation_uses_production_prefix_and_compares_reviewed_budget(tmp_path):
    from evals.always_memory import evaluate_always, scripted_complete
    result = asyncio.run(evaluate_always(scripted_complete, tmp_path))
    assert result['adherence'] == {'before': 1, 'after': 1}
    before = [row for row in result['results'] if row['mode'] == 'before']
    after = [row for row in result['results'] if row['mode'] == 'after']
    assert all(row['always_tokens'] > 1500 for row in before)
    assert all(row['always_tokens'] <= 1500 for row in after)


@pytest.mark.parametrize('value', [0, 1500, 3000, -1, True, 1.5])
def test_budget_configuration_validation(tmp_path, value):
    path = tmp_path / 'config.json'
    import json
    from momoi.config.workspace import bootstrap
    bootstrap(path)
    raw = json.loads(path.read_text())
    raw['memory'] = {'always_token_budget': value}
    path.write_text(json.dumps(raw))
    if type(value) is not int or value < 0:
        with pytest.raises(ConfigError):
            load_config(path)
    else:
        assert load_config(path).memory.always_token_budget == value


@pytest.mark.parametrize('succeed', [True, False])
def test_reply_declared_memory_use_requires_visible_id_and_success(daemon, succeed):
    from unittest.mock import AsyncMock
    from momoi.models import IncomingMessage, ProviderResponse, ToolCall
    from momoi.runtime.agent import TurnExecutionSpec
    from tests.support import reply_call

    visible = seed(daemon.memory, 'visible', [], activation='always')
    invisible = seed(daemon.memory, 'invisible', [])
    event = IncomingMessage('use-event', 'use-event', '你好', time.time(), time.time(), channel=daemon.channel.name)
    daemon.store.add_event(event)
    daemon.store.begin_turn('use-turn', 'owner', ['use-event'])
    messages = daemon.shared_turn_context('use-turn')['messages']
    calls = [reply_call('use-reply', bubbles=['阿澈，早呀'])]
    calls[0].arguments['used_memory_ids'] = [visible, invisible, 9999]
    rounds = 0
    async def complete(*args, **kwargs):
        nonlocal rounds
        call = calls[0] if rounds == 0 else ToolCall('end', 'end_turn', {'mood': {'decision': 'unchanged'}})
        rounds += 1
        return ProviderResponse([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.arguments}], [call])
    if not succeed:
        daemon.tool_batch.replyer.generate = AsyncMock(side_effect=ValueError('cannot express reply'))
    daemon.provider = type('Provider', (), {'complete': staticmethod(complete)})()
    draft = TurnDraft()
    asyncio.run(daemon._run_tool_loop(daemon._system(planner=True), messages,
        daemon.tool_surface.conversation_specs(), [event], draft,
        execution=TurnExecutionSpec('owner', permitted_tools=daemon.tool_surface.permitted_names('owner')),
        source_event_id='use-event', turn_id='use-turn', delivery_channel=daemon.channel))
    used = [item['memory_id'] for item in draft.memory_access if item['kind'] == 'used']
    assert used == ([visible] if succeed else [])
    assert not daemon.store._db.execute('SELECT 1 FROM memory_reply_usage').fetchone()
    daemon.store.commit_turn([event], '你好', AgentReply([]), draft, turn_id='use-turn')
    assert [row[0] for row in daemon.store._db.execute('SELECT memory_id FROM memory_reply_usage')] == used


def test_reply_usage_retention_preserves_sixty_day_window(store):
    now = time.time()
    identifier = seed(store.memories, 'usage', [])
    with store.transaction():
        store._commit_memory_access('retention', [
            {'memory_id': identifier, 'kind': 'used', 'event_key': str(days), 'created_at': now-days*DAY}
            for days in (181, 60, 1)])
    store.compact_memory_access(now=now)
    rows = store._db.execute('SELECT event_key FROM memory_reply_usage ORDER BY created_at').fetchall()
    assert [row[0] for row in rows] == ['60', '1']


@pytest.mark.parametrize('kind', ['profile', 'preference'])
def test_stable_always_kinds_require_sixty_days_without_use(store, kind):
    now = time.time()
    identifier = seed(store.memories, 'stable', [], activation='always', content='用户不吃香菜，称呼为阿澈')
    with store.transaction():
        store._db.execute('UPDATE memories SET kind=? WHERE id=?', (kind, identifier))
        store._db.execute('UPDATE memory_usage_observation SET started_at=?', (now,))
    for days in (0, 7, 59):
        assert store.generate_memory_activation_suggestions(now=now + days*DAY) == []
        assert store.memory_activation_suggestions()['items'] == []
    candidates = store.generate_memory_activation_suggestions(now=now + 61*DAY)
    assert [(item['memory_id'], item['target']) for item in candidates] == [(identifier, 'recall')]
    assert '60 天' in candidates[0]['reason']


@pytest.mark.parametrize('kind', ['profile', 'preference'])
def test_newly_promoted_or_updated_stable_memory_is_not_recommended_for_demotion(store, kind):
    now = time.time()
    identifier = seed(store.memories, 'stable', [], content='用户不吃香菜，称呼为阿澈')
    with store.transaction():
        store._db.execute('UPDATE memories SET kind=? WHERE id=?', (kind, identifier))
    add_usage(store, identifier, now)
    store.generate_memory_activation_suggestions(now=now)
    item = store.memory_activation_suggestions()['items'][0]
    assert item['target'] == 'always'
    result = store.review_memory_activation_suggestion(item['id'], approve=True)
    assert store.generate_memory_activation_suggestions(now=now + 7*DAY) == []
    store.update_memory_content(result['memory_id'], '称呼用户为阿澈，点餐时不要香菜')
    assert store.generate_memory_activation_suggestions(now=now + 14*DAY) == []
    assert store.memory_activation_suggestions()['items'] == []
