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
        assert len(matched) == 4
        assert set(matched[0]) == {'id', 'title', 'status', 'updated_at'}
        assert len(store.triggered_episodes('时之笛', limit=1)) == 1
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
        assert store.triggered_episodes('a.b')[0]['title'] == 'literal'
        assert store.triggered_episodes('switch')[0]['id'] == 'literal'
        with pytest.raises(ValueError):
            store.set_episode_triggers('literal', ['a', 'b', 'c'])
    finally:
        store.close()


def test_topic_results_are_capped_at_five_without_summary(tmp_path):
    store = Store(tmp_path / 'cap.sqlite3', tmp_path)
    try:
        for i in range(9):
            store.create_episode(f'事实 {i}', episode_id=str(i))
            store.set_episode_triggers(str(i), ['对象'])
        results = store.triggered_episodes('对象', limit=100)
        assert len(results) == 5
        assert all(set(row) == {'id', 'title', 'status', 'updated_at'} for row in results)
    finally:
        store.close()


@pytest.mark.parametrize('stage', ['owner', 'goal', 'webhook', 'heartbeat'])
def test_reply_trigger_routing_and_dedupe(tmp_path, stage):
    from momoi.runtime.context.triggers import attach_reply_episode_triggers
    from unittest.mock import patch
    store = Store(tmp_path / 'route.sqlite3', tmp_path)
    try:
        store.create_episode('答应送我的礼物', episode_id='gift')
        store.set_episode_triggers('gift', ['时之笛'])
        seen = set()
        with patch.object(store, 'triggered_episodes', wraps=store.triggered_episodes) as lookup:
            result = {'state': 'sent', 'bubbles': ['时之笛的邮件到了']}
            attach_reply_episode_triggers(store, result, stage=stage, seen=seen)
            if stage == 'owner':
                lookup.assert_not_called()
                assert 'triggered_episodes' not in result
            else:
                assert [(row['id'], row['title'], row['status']) for row in result['triggered_episodes']] == [('gift', '答应送我的礼物', 'open')]
                again = {'bubbles': ['时之笛']}
                attach_reply_episode_triggers(store, again, stage=stage, seen=seen)
                assert 'triggered_episodes' not in again
    finally:
        store.close()


def test_owner_episode_context_escapes_titles_and_ignores_notices(tmp_path):
    from momoi.models import IncomingMessage
    from momoi.runtime.context.triggers import triggered_episode_context
    from xml.etree import ElementTree
    store = Store(tmp_path / 'owner.sqlite3', tmp_path)
    try:
        store.create_episode('送我的礼物 <fake/>', episode_id='gift')
        store.set_episode_triggers('gift', ['时之笛'])
        event = IncomingMessage('owner', 'owner', '时之笛', 1, 1)
        node = ElementTree.fromstring(triggered_episode_context(store, [event]))
        attrib = node.find('episode').attrib
        assert (attrib['id'], attrib['status']) == ('gift', 'open')
        assert attrib['updated_at']
        assert node.find('episode/title').text == '送我的礼物 <fake/>'
        assert node.find('.//fake') is None
        notice = IncomingMessage('notice', 'owner', '时之笛', 1, 1, delivery_context={'channel_notice': 'recall'})
        assert triggered_episode_context(store, [notice]) == ''
    finally:
        store.close()


def test_trigger_brings_follow_up_and_revision_graph(tmp_path):
    from momoi.models import IncomingMessage
    from momoi.runtime.context.triggers import triggered_episode_context, attach_reply_episode_triggers
    from xml.etree import ElementTree
    store = Store(tmp_path / 'graph.sqlite3', tmp_path)
    try:
        for identifier, title, summary in [
            ('a', 'GTA6 预载', '主人预载了 GTA6'), ('b', 'PS5 开玩', '在 PS5 上开玩 GTA6'),
            ('c', '30 帧', '抱怨 30 帧但喜欢自在一日'), ('d', '旧计划', '原定周末玩'),
            ('e', '新计划', '改到周五玩'),
        ]:
            store.create_episode(title, episode_id=identifier)
            store._db.execute(
                "UPDATE conversation_episodes SET narrative_summary=?, summarized_through_ordinal=1 WHERE id=?",
                (summary, identifier))
        store.set_episode_triggers('a', ['GTA6'])
        store.set_episode_triggers('d', ['周末'])
        with store._db:
            for source, target, relation in [('b', 'a', 'follows_up'), ('c', 'b', 'follows_up'), ('e', 'd', 'revises')]:
                store._db.execute(
                    """INSERT INTO episode_relations
                       (source_episode_id,target_episode_id,relation,explanation,
                        evidence_json,source_summary_ordinal,created_at,updated_at)
                       VALUES (?,?,?,'后续','{}',1,1,1)""", (source, target, relation))
        event = IncomingMessage('m', 'owner', 'gta6 周末', 1, 1)
        node = ElementTree.fromstring(triggered_episode_context(store, [event]))
        episodes = {item.attrib['id']: item for item in node.findall('episode')}
        assert set(episodes) == {'a', 'b', 'c', 'd', 'e'}
        assert episodes['b'].attrib['injected'] == 'follows_up'
        assert episodes['c'].find('summary').text == '抱怨 30 帧但喜欢自在一日'
        assert 'status' not in episodes['c'].attrib
        assert episodes['e'].attrib['injected'] == 'revises'
        assert episodes['d'].find('revised_by').attrib['id'] == 'e'
        seen = set()
        result = {'bubbles': ['GTA6 怎么样']}
        attach_reply_episode_triggers(store, result, stage='heartbeat', seen=seen)
        assert [row['id'] for row in result['triggered_episodes']] == ['a', 'b', 'c']
        assert seen == {'a', 'b', 'c'}
    finally:
        store.close()
