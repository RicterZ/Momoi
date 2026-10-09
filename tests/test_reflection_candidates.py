import json
from copy import deepcopy
from unittest.mock import patch

import pytest

from momoi.config.models import ReflectionConfig
from momoi.storage import Store
from tests.test_weekly_reflection_storage import seed, stamp


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'db', timezone='Asia/Shanghai')
    yield value
    value.close()


def review(store, end, days, *, key='quiet', previous=(), content='老师选座时偏好安静的位置'):
    for day in days:
        seed(store, day, content, key=key)
    row = store.claim_due_weekly_reflection(ReflectionConfig(enabled=True), stamp(end + 'T05:00:00'))
    source = json.loads(row['input_json'])
    events = [{'refs': [item['id']], 'summary': '一次独立选座'}
              for day in source['days'] for item in day['observations']]
    events += [{'refs': [item['id']], 'summary': item['summary']} for item in previous]
    finding = {'key': key, 'kind': 'preference', 'content': content, 'events': events, 'conflicts': []}
    store.commit_weekly_reflection(end, 'unused', {'summary': '累计观察', 'findings': [finding]})
    return finding


def test_cross_week_counts_expire_and_do_not_enter_memory(store):
    review(store, '2026-10-11', ['2026-10-06', '2026-10-07'])
    old = store.reflection_candidates(end='2026-10-11')[0]
    assert old['count'] == 2 and old['status'] == 'observation'
    # Omitted old events survive; new week carries compact observations instead of old daily transcripts.
    review(store, '2026-10-18', ['2026-10-12', '2026-10-13', '2026-10-14'])
    source = json.loads(store.weekly_reflection('2026-10-18')['input_json'])
    assert len(source['previous_candidates'][0]['events']) == 2
    assert source['days'][0]['date'] == '2026-10-11'
    candidate = store.reflection_candidates(end='2026-10-18')[0]
    assert candidate['count'] == 5 and candidate['status'] == 'pending'
    assert store.memories.repository.inventory() == []
    assert store.reflection_candidates(end='2026-11-08')[0]['count'] == 3
    assert store.reflection_candidates(end='2026-11-15') == []


def test_edit_delete_and_admit_are_revision_checked_and_atomic(store):
    review(store, '2026-10-11', ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09'])
    item = store.reflection_candidates(end='2026-10-11')[0]
    with patch('momoi.storage.reflection.candidates.datetime') as clock:
        clock.now.return_value.date.return_value.isoformat.return_value = '2026-10-11'
        store.change_reflection_candidate(item['id'], item['revision'], content='老师选座时喜欢安静一点')
        with pytest.raises(ValueError, match='changed'):
            store.change_reflection_candidate(item['id'], item['revision'], admit=True)
        current = store.reflection_candidates(end='2026-10-11')[0]
        result = store.change_reflection_candidate(current['id'], current['revision'], admit=True)
        assert store.change_reflection_candidate(current['id'], current['revision'], admit=True) == result
        assert store.reflection_candidates() == []
    memories = store.memories.repository.inventory()
    assert len(memories) == 1 and memories[0]['activation'] == 'recall'
    assert memories[0]['content'] == '老师选座时喜欢安静一点'
    assert memories[0]['source_event_id'].startswith('dashboard:reflection:')
    assert memories[0]['meta']['triggers'] == []


def test_frozen_week_cannot_overwrite_edit_or_revive_deleted_candidate(store):
    review(store, '2026-10-11', ['2026-10-06', '2026-10-07'])
    original = store.reflection_candidates(end='2026-10-11')[0]
    seed(store, '2026-10-12', '本周选座')
    record = store.claim_due_weekly_reflection(ReflectionConfig(enabled=True), stamp('2026-10-18T05:00:00'))
    source = json.loads(record['input_json'])
    finding = {'key': 'quiet', 'kind': 'preference', 'content': '模型想改写',
               'events': [{'refs': [source['days'][1]['observations'][0]['id']], 'summary': '新选座'}], 'conflicts': []}
    store.change_reflection_candidate(original['id'], original['revision'], content='用户修订正文')
    store.commit_weekly_reflection('2026-10-18', 'unused', {'summary': '归纳', 'findings': [finding]})
    current = store.reflection_candidates(end='2026-10-18')[0]
    assert current['content'] == '用户修订正文' and current['count'] == 3
    store.change_reflection_candidate(current['id'], current['revision'], delete=True)
    with store.transaction():
        store.apply_reflection_candidates(source, [finding])
    assert store.reflection_candidates(end='2026-10-18') == []


def test_remention_does_not_refresh_date_or_allow_split_support(store):
    review(store, '2026-10-11', ['2026-10-06'])
    assert store.reflection_candidates(end='2026-10-11') == []
    previous = store.reflection_candidates(end='2026-10-11', include_seeds=True)[0]
    seed(store, '2026-10-12', '又聊起上次选座')
    source = store.weekly_reflection_source('2026-10-18')
    new = source['days'][1]['observations'][0]['id']
    finding = {'key': 'quiet', 'kind': 'preference', 'content': previous['content'],
               'events': [{'refs': [previous['events'][0]['id'], new], 'summary': '同一次选座'}], 'conflicts': []}
    with store.transaction():
        store.apply_reflection_candidates(source, [finding])
    item = store.reflection_candidates(end='2026-10-18', include_seeds=True)[0]
    assert item['count'] == 1 and item['events'][0]['date'] == '2026-10-06'
    bad = deepcopy(finding)
    bad['events'].append({'refs': [new], 'summary': '不能拆成第二次'})
    with pytest.raises(ValueError, match='counted_twice'), store.transaction():
        store.apply_reflection_candidates(source, [bad])
    assert store.reflection_candidates(end='2026-10-18', include_seeds=True)[0] == item


def test_expired_or_conflicting_candidate_cannot_be_admitted(store):
    review(store, '2026-10-11', ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09'])
    item = store.reflection_candidates(end='2026-10-11')[0]
    with patch('momoi.storage.reflection.candidates.datetime') as clock:
        clock.now.return_value.date.return_value.isoformat.return_value = '2026-11-12'
        with pytest.raises(ValueError, match='not_ready'):
            store.change_reflection_candidate(item['id'], item['revision'], admit=True)
        clock.now.return_value.date.return_value.isoformat.return_value = '2026-10-11'
        with store.transaction():
            store._db.execute('UPDATE reflection_candidates SET conflicts_json=?', (json.dumps([item['events'][0]]),))
        with pytest.raises(ValueError, match='not_ready'):
            store.change_reflection_candidate(item['id'], item['revision'], admit=True)
    assert store.memories.repository.inventory() == []


def test_weekly_commit_rolls_back_all_candidates_and_rejects_unknown_references(store):
    seed(store, '2026-10-06')
    row = store.claim_due_weekly_reflection(ReflectionConfig(enabled=True), stamp('2026-10-11T05:00:00'))
    source = json.loads(row['input_json'])
    ref = source['days'][2]['observations'][0]['id']
    good = {'key': 'quiet', 'kind': 'preference', 'content': '偏好安静',
            'events': [{'refs': [ref], 'summary': '独立选座'}], 'conflicts': []}
    bad = {**good, 'key': 'other', 'events': [{'refs': ['missing'], 'summary': '编造'}]}
    with pytest.raises(ValueError, match='unknown_observation'):
        store.commit_weekly_reflection('2026-10-11', 'unused', {'summary': '失败', 'findings': [good, bad]})
    assert store.reflection_candidates(end='2026-10-11', include_seeds=True) == []
    assert store.weekly_reflection('2026-10-11')['state'] == 'running'


def test_dashboard_candidate_auth_revision_and_manual_admission(store):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    from momoi.dashboard.app import create_dashboard_app
    from momoi.dashboard.auth import issue_dashboard_jwt
    from momoi.dashboard.settings import DashboardSettings

    review(store, '2026-10-11', ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09'])

    async def run():
        async with TestClient(TestServer(create_dashboard_app(store, token='secret', settings=DashboardSettings(())))) as client:
            url = '/api/reflection-candidates'
            assert (await client.get(url)).status == 401
            client.session.headers['Authorization'] = 'Bearer ' + issue_dashboard_jwt('secret')
            data = await (await client.get(url)).json()
            item = data['items'][0]
            path = f"{url}/{item['id']}"
            assert (await client.post(path + '/admit', json={})).status == 400
            assert (await client.patch(path, json={'revision': item['revision'], 'content': '修订偏好'})).status == 200
            assert (await client.post(path + '/admit', json={'revision': item['revision']})).status == 409
            assert (await client.post(path + '/admit', json={'revision': item['revision'] + 1})).status == 200
            assert (await (await client.get(url)).json())['items'] == []
            assert (await client.delete(path, json={'revision': item['revision']})).status == 409
    with patch('momoi.storage.reflection.candidates.datetime') as clock:
        clock.now.return_value.date.return_value.isoformat.return_value = '2026-10-11'
        asyncio.run(run())


def test_manual_admission_is_not_blocked_by_previously_deleted_memory(store):
    import hashlib
    from tests.test_memory_repository import write
    review(store, '2026-10-11', ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09'])
    item = store.reflection_candidates(end='2026-10-11')[0]
    key = 'reflection.' + hashlib.sha256(item['key'].encode()).hexdigest()[:24]
    old = write(store.memories.repository, key=key, text=item['content'])
    store.forget_memory_by_id(old, '删除旧记录')
    with patch('momoi.storage.reflection.candidates.datetime') as clock:
        clock.now.return_value.date.return_value.isoformat.return_value = '2026-10-11'
        result = store.change_reflection_candidate(item['id'], item['revision'], admit=True)
    assert result['memory_id'] != old
    assert not store.memories.snapshots([old])
    assert store.memories.snapshots([result['memory_id']])
