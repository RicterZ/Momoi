import sqlite3

import pytest

from momoi.storage import Store
from momoi.storage.core.migrations import _add_episode_triggers
from tests.support import config
from tests.test_episode_annealing import add_turn
from momoi.runtime import MomoiDaemon


def test_additive_migration_preserves_topics():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE conversation_episodes(id TEXT, topics_json TEXT)")
    db.execute("INSERT INTO conversation_episodes VALUES ('old', '[\"邮箱检查\"]')")
    _add_episode_triggers(db)
    _add_episode_triggers(db)
    assert db.execute("SELECT topics_json, triggers_json, triggers_manual FROM conversation_episodes").fetchone() == ('["邮箱检查"]', '[]', 0)


def test_summary_generates_triggers_and_preserves_manual_override(tmp_path):
    daemon = MomoiDaemon(config(str(tmp_path)))
    store = daemon.store
    try:
        store.create_episode("礼物", episode_id="episode-main")
        add_turn(daemon, 1)
        row = store._db.execute("SELECT id, content FROM messages WHERE role='user'").fetchone()
        claims = [dict(message_id=row['id'], turn_id='turn-1', ordinal=1, quote=row['content'])]
        def finish(words):
            with store._db:
                store._db.execute("UPDATE conversation_episodes SET summary_claimed_at=1 WHERE id='episode-main'")
            store.finish_episode_annealing('episode-main', 1, claims, triggers=words)
        finish(['时之笛'])
        assert store.episode('episode-main')['triggers'] == ['时之笛']
        store.set_episode_triggers('episode-main', ['時之笛'])
        finish(['其他词'])
        assert store.episode('episode-main')['triggers'] == ['時之笛']
        store.set_episode_triggers('episode-main', [])
        finish(['时之笛'])
        assert store.episode('episode-main')['triggers'] == []
    finally:
        store.close()


def test_deterministic_matching_limits_and_persistence(tmp_path):
    path = tmp_path / 'momoi.sqlite3'
    store = Store(path, tmp_path)
    for i in range(4):
        episode_id = str(i)
        store.create_episode('礼物', episode_id=episode_id, topics=['邮箱检查'])
        store.set_episode_triggers(episode_id, ['时之笛', 'Switch 2'])
        with store._db:
            store._db.execute("UPDATE conversation_episodes SET narrative_summary=? WHERE id=?", ('用户想送我时之笛', episode_id))
    store.close()
    store = Store(path, tmp_path)
    try:
        matched = store.triggered_episodes('時之笛预购：ＳＷＩＴＣＨ ２')
        assert len(matched) == 3
        assert matched[0]['matched_triggers'] == ['Switch 2']
        assert matched[0]['summary'] == '用户想送我时之笛'
        assert len(store.triggered_episodes('时之笛', limit=1)) == 1
        assert store.triggered_episodes('时之笛', token_budget=1) == []
        assert store.triggered_episodes('邮箱检查') == []
        assert store.triggered_episodes('时之笛', limit=0) == []
        with pytest.raises(ValueError):
            store.triggered_episodes(None)
    finally:
        store.close()


def test_episode_triggers_are_literal_and_limited(tmp_path):
    store = Store(tmp_path / 'literal.sqlite3', tmp_path)
    try:
        store.create_episode('literal', episode_id='literal')
        store.set_episode_triggers('literal', ['a.b', 'ＳＷＩＴＣＨ'])
        with store._db:
            store._db.execute("UPDATE conversation_episodes SET narrative_summary='summary' WHERE id='literal'")
        assert store.triggered_episodes('axb') == []
        assert store.triggered_episodes('a.b')[0]['matched_triggers'] == ['a.b']
        assert store.triggered_episodes('switch')[0]['matched_triggers'] == ['ＳＷＩＴＣＨ']
        with pytest.raises(ValueError):
            store.set_episode_triggers('literal', ['a', 'b', 'c'])
    finally:
        store.close()
