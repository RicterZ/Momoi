from momoi.config.models import HeartbeatConfig, NotificationConfig
from momoi.models import AgentReply
from momoi.storage import Store
from momoi.storage.core.migrations import SCHEMA_VERSION


def test_upgrade_cancels_old_followup_and_preserves_history(tmp_path):
    path = tmp_path / 'momoi.sqlite3'
    store = Store(path)
    store.commit_turn([], '老师的消息', AgentReply(['原始回复']), turn_id='owner')
    store.create_episode('旧等待', episode_id='waiting')
    store.link_turn_to_episode('waiting', 'owner')
    store.create_episode('有未解决事项', episode_id='open-loop', open_loops=['待确认'])
    store.link_turn_to_episode('open-loop', 'owner')
    owner_outbox = store.due_outbox()[0].id
    store.mark_sent(owner_outbox)
    for tid in ('delivered', 'queued', 'running', 'uncertain'):
        store.begin_turn(tid, 'reply_followup', [], parent_turn_id='owner')
        store.queue_progress(tid, 'reply', ['旧跟进'], 'napcat')
    store._archive_progress_messages('queued', '[]')
    store._archive_progress_messages('uncertain', '[]')
    store._db.execute("UPDATE outbox SET state='ambiguous' WHERE turn_id='uncertain'")
    delivered = store._db.execute("SELECT id FROM outbox WHERE turn_id='delivered'").fetchone()[0]
    store.mark_sent(delivered)
    store._db.execute("UPDATE turns SET state='completed' WHERE id IN ('delivered','queued')")
    store._db.execute("UPDATE self_state SET next_heartbeat_at=9000, activity='读书'")
    # Recreate the persisted scheduling fields from the previous schema.
    for column, declaration in (
        ('pending_reply_turn_id', 'TEXT'),
        ('pending_reply_expectation', "TEXT NOT NULL DEFAULT ''"),
        ('pending_reply_next_check_at', 'REAL'),
    ):
        store._db.execute(f'ALTER TABLE self_state ADD COLUMN {column} {declaration}')
    store._db.execute("UPDATE self_state SET pending_reply_turn_id='owner', pending_reply_expectation='回答', pending_reply_next_check_at=1")
    for table in ('outbox', 'notifications'):
        store._db.execute(f"ALTER TABLE {table} ADD COLUMN reply_expectation TEXT NOT NULL DEFAULT ''")
    store._db.execute("UPDATE outbox SET reply_expectation='old wait'")
    store._db.execute("""INSERT INTO notifications
        (id,turn_id,goal_id,notification_key,priority,reason,messages_json,state,not_before,created_at)
        VALUES ('old','running','heartbeat','heartbeat.reply_followup','normal','旧跟进','[]','pending',1,1)""")
    for tid in ('queued', 'delivered'):
        store._db.execute("""INSERT INTO notifications
            (id,turn_id,goal_id,notification_key,priority,reason,messages_json,state,not_before,created_at)
            VALUES (?,?,'heartbeat','heartbeat.reply_followup','normal','旧跟进','[]','queued',1,1)""",
            (tid, tid))
    store._db.execute(f'PRAGMA user_version={SCHEMA_VERSION - 1}')
    store._db.commit()
    store.close()

    for _ in range(2):
        store = Store(path)
        assert store.due_outbox() == []
        assert store._db.execute("SELECT state FROM outbox WHERE id=?", (delivered,)).fetchone()[0] == 'sent'
        assert store._db.execute("SELECT state FROM turns WHERE id='running'").fetchone()[0] == 'cancelled'
        assert store._db.execute("SELECT state FROM notifications WHERE id='old'").fetchone()[0] == 'superseded'
        assert store._db.execute("SELECT state FROM notifications WHERE id='queued'").fetchone()[0] == 'superseded'
        assert store._db.execute("SELECT state FROM notifications WHERE id='delivered'").fetchone()[0] == 'queued'
        assert store._db.execute("SELECT content FROM messages WHERE turn_id='owner' AND role='user'").fetchone()[0] == '老师的消息'
        assert not any(key.startswith('pending_reply_') for key in store.self_state())
        for table in ('outbox', 'notifications'):
            assert 'reply_expectation' not in {row[1] for row in store._db.execute(f'PRAGMA table_info({table})')}
        assert store.self_state()['activity'] == '读书'
        assert store.episode('waiting')['status'] == 'closing'
        assert store.episode('open-loop')['status'] == 'open'
        assert store._db.execute("SELECT delivery_state FROM messages WHERE turn_id='queued'").fetchone()[0] == 'failed'
        assert store._db.execute("SELECT delivery_state FROM messages WHERE turn_id='uncertain'").fetchone()[0] == 'uncertain'
        assert store.next_heartbeat_due_at(True) == 9000
        assert store.next_heartbeat_due_at(False) is None
        assert store.claim_due_heartbeat(HeartbeatConfig(enabled=False), NotificationConfig(), now=10000) is None
        assert store._db.execute('PRAGMA foreign_key_check').fetchall() == []
        store.close()


def test_delivery_never_schedules_a_followup(tmp_path):
    store = Store(tmp_path / 'momoi.sqlite3')
    try:
        store.commit_turn([], '问题', AgentReply(['你想选哪个？']), turn_id='owner')
        store.mark_sent(store.due_outbox()[0].id)
        assert store.next_heartbeat_due_at(False) is None
        assert not hasattr(store, 'pending_owner_reply')
        assert not hasattr(store, 'commit_reply_followup')
    finally:
        store.close()


def test_retired_stage_in_existing_config_does_not_block_upgrade(tmp_path):
    from tests.support import write_app_config
    from momoi.config.loading import load_config
    from momoi.config.manager import ConfigurationManager

    (tmp_path / 'prompts').mkdir()
    (tmp_path / 'prompts/SOUL.md').write_text('Test soul')
    (tmp_path / 'mcp.json').write_text('{"mcpServers": {}}')
    path = tmp_path / 'config.json'
    write_app_config(path, {
        'providers': 'providers.yaml',
        'channels': {'enabled': {}}, 'context': {}, 'logging': {},
        'storage': {'database': 'momoi.sqlite3'},
        'thinking': {'stages': {'reply_followup': 'low', 'owner': 'high'}},
    })
    assert 'reply_followup' not in load_config(path).thinking_stages
    manager = ConfigurationManager(path)
    assert manager.read_app()['thinking']['stages'] == {'owner': 'high'}
    assert 'reply_followup' not in manager.validate().thinking_stages
