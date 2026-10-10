import json
from datetime import datetime
from zoneinfo import ZoneInfo

from momoi.config.models import ReflectionConfig
from momoi.storage import Store


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


def seed(store, day, content='当天观察', key='topic'):
    now = stamp(day + 'T12:00:00')
    with store._db:
        store._db.execute(
            "INSERT INTO reflections (id,local_date,state,scheduled_at,created_at) VALUES (?,?,'running',?,?)",
            (f'reflection:{day}', day, now, now),
        )
    store.commit_reflection(day, 'unused', '日记', [{
        'kind': 'profile', 'key': key, 'content': content, 'evidence': content, 'confidence': 0.7,
    }])


def test_weekly_schedule_snapshot_retry_restart_and_no_promotion(tmp_path):
    path = tmp_path / 'db'
    store = Store(path, timezone='Asia/Shanghai')
    enabled = ReflectionConfig(enabled=True)
    seed(store, '2026-10-06')
    seed(store, '2026-10-07')
    # The current week is not yet eligible before Sunday daily review time + one hour.
    assert store._weekly_reflection_slot(stamp('2026-10-11T03:59:59'), enabled).date().isoformat() == '2026-10-04'
    assert store.claim_due_weekly_reflection(ReflectionConfig(), stamp('2026-10-11T05:00:00')) is None
    row = store.claim_due_weekly_reflection(enabled, stamp('2026-10-11T05:00:00'))
    assert row['period_end'] == '2026-10-11'
    source = json.loads(row['input_json'])
    assert len(source['days']) == 7 and source['period_start'] == '2026-10-04'
    assert len([d for d in source['days'] if d['state'] == 'missing']) == 5
    assert source['days'][2]['observations'][0]['key'] == 'topic'
    assert store.claim_due_weekly_reflection(enabled, stamp('2026-10-11T06:00:00')) is None
    assert store.next_weekly_reflection_due_at(enabled, stamp('2026-10-11T06:00:00')) is None
    store.close()
    store = Store(path, timezone='Asia/Shanghai')
    assert store.weekly_reflection('2026-10-11')['state'] == 'pending'
    seed(store, '2026-10-08', '重试时新增加的观察')
    row2 = store.claim_due_weekly_reflection(enabled, stamp('2026-10-11T06:00:00'))
    assert row2['input_json'] == row['input_json']
    store.begin_turn('weekly', 'weekly_reflection', ['weekly-reflection:2026-10-11'])
    store.commit_weekly_reflection('2026-10-11', 'weekly', {'summary': '盘点', 'findings': []})
    assert store.next_weekly_reflection_due_at(enabled, stamp('2026-10-11T06:00:00')) == stamp('2026-10-18T04:00:00')
    assert store._db.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 0
    store.close()


def test_weekly_excludes_deleted_observations_and_waits_for_daily(tmp_path):
    store = Store(tmp_path / 'db', timezone='Asia/Shanghai')
    seed(store, '2026-10-10')
    memory = store.list_reflection_memories()[0]
    store.delete_reflection_memory(memory['id'])
    assert not store.weekly_reflection_source('2026-10-11')['days'][-1]['observations']
    with store._db:
        store._db.execute("UPDATE reflections SET state='running'")
    assert store.claim_due_weekly_reflection(ReflectionConfig(enabled=True), stamp('2026-10-11T05:00:00')) is None
    store.close()




def test_overdue_week_is_retried_after_next_sunday(tmp_path):
    from unittest.mock import patch
    store = Store(tmp_path / 'db', timezone='Asia/Shanghai')
    enabled = ReflectionConfig(enabled=True)
    now = stamp('2026-10-11T05:00:00')
    first = store.claim_due_weekly_reflection(enabled, now)
    with patch('momoi.storage.reflection.weekly.time.time', return_value=now):
        store.release_weekly_reflection('2026-10-11', 'failure')
    due = store.next_weekly_reflection_due_at(enabled, stamp('2026-10-18T05:00:00'))
    assert due == now + 900
    retried = store.claim_due_weekly_reflection(enabled, stamp('2026-10-18T05:00:00'))
    assert retried['period_end'] == first['period_end']
    assert retried['input_json'] == first['input_json']
    store.close()


def test_daily_triggers_survive_restart_and_reach_weekly_input(tmp_path):
    from momoi.runtime.parsing import parse_reflection_finish
    from momoi.runtime.workflows.weekly_reflection import weekly_reflection_input
    path = tmp_path / 'db'
    store = Store(path, timezone='Asia/Shanghai')
    seed(store, '2026-10-06')
    item = dict(kind='preference', key='sleep', content='用户期待晚安仪式',
                evidence='晚安', confidence=0.9, triggers=['晚安'])
    args = dict(summary='复盘', memories=[item])
    result, error = parse_reflection_finish(args, source='晚安', owner_source='晚安', knowledge_source='')
    assert error is None
    store.commit_reflection('2026-10-06', 'unused', result['summary'], result['memories'])
    store.close()
    store = Store(path, timezone='Asia/Shanghai')
    source = store.weekly_reflection_source('2026-10-11')
    assert source['days'][2]['observations'][0]['triggers'] == ['晚安']
    assert '<triggers>["晚安"]</triggers>' in weekly_reflection_input(source)
    store.close()
    item['triggers'] = ['晚安', '亲亲', '睡觉']
    assert parse_reflection_finish(args, source='晚安', owner_source='晚安', knowledge_source='')[1]


def test_daily_trigger_migration_preserves_existing_observations(tmp_path):
    from momoi.storage.core.migrations import _add_daily_reflection_triggers
    store = Store(tmp_path / 'db', timezone='Asia/Shanghai')
    seed(store, '2026-10-06')
    with store._db:
        store._db.execute('ALTER TABLE reflection_memories DROP COLUMN triggers_json')
        _add_daily_reflection_triggers(store._db)
        _add_daily_reflection_triggers(store._db)
    observation = store.weekly_reflection_source('2026-10-11')['days'][2]['observations'][0]
    assert observation['content'] == '当天观察'
    assert observation['triggers'] == []
    store.close()


def test_weekly_follows_daily_time_and_keeps_sunday_period_across_midnight(tmp_path):
    store = Store(tmp_path / 'db', timezone='Asia/Shanghai')
    try:
        config = ReflectionConfig(enabled=True, at='23:30')
        before = stamp('2026-10-12T00:29:59')
        due = stamp('2026-10-12T00:30:00')
        assert store._weekly_reflection_slot(before, config).timestamp() == stamp('2026-10-05T00:30:00')
        assert store._weekly_reflection_slot(due, config).timestamp() == due
        row = store.claim_due_weekly_reflection(config, due)
        assert row['period_end'] == '2026-10-11'
        assert row['scheduled_at'] == due
        disabled = ReflectionConfig(enabled=False, at='23:30')
        assert store.claim_due_weekly_reflection(disabled, due) is None
        assert store.next_weekly_reflection_due_at(disabled, due) is None
        morning = ReflectionConfig(enabled=True, at='07:15')
        assert store._weekly_reflection_slot(stamp('2026-10-11T08:15:00'), morning).timestamp() == stamp('2026-10-11T08:15:00')
    finally:
        store.close()
