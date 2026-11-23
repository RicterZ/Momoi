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


def _activity(store, episode_id, at):
    with store._db:
        store._db.execute("UPDATE conversation_episodes SET created_at=? WHERE id=?", (at, episode_id))


def _relate(store, source, target, relation):
    with store._db:
        store._db.execute(
            """INSERT INTO episode_relations
               (source_episode_id,target_episode_id,relation,explanation,
                evidence_json,source_summary_ordinal,created_at,updated_at)
               VALUES (?,?,?,'关联','{}',1,1,1)""", (source, target, relation))


def _topics(store, *rows):
    for identifier, at in rows:
        store.create_episode(f'话题 {identifier}', episode_id=identifier)
        with store._db:
            store._db.execute(
                "UPDATE conversation_episodes SET narrative_summary=?, summarized_through_ordinal=1 WHERE id=?",
                (f'摘要 {identifier}', identifier))
        _activity(store, identifier, at)


def _slots(store, text):
    from momoi.models import IncomingMessage
    from momoi.runtime.context.triggers import triggered_episode_context
    from xml.etree import ElementTree
    context = triggered_episode_context(store, [IncomingMessage('m', 'owner', text, 1, 1)])
    return ElementTree.fromstring(context).findall('episode') if context else []


def test_trigger_group_is_shown_as_newest_direct_follow_up(tmp_path):
    store = Store(tmp_path / 'group.sqlite3', tmp_path)
    try:
        _topics(store, ('root', 1), ('next', 2), ('later', 3))
        _relate(store, 'next', 'root', 'follows_up')
        _relate(store, 'later', 'next', 'follows_up')
        store.set_episode_triggers('root', ['alpha'])
        store.set_episode_triggers('next', ['alpha'])
        slots = _slots(store, 'alpha')
        assert [item.attrib['id'] for item in slots] == ['later']
        assert [item.attrib['id'] for item in slots[0].findall('supplement')] == ['next', 'root']
        assert slots[0].find('supplement/summary').text == '摘要 next'
        assert slots[0].attrib['status'] == 'open'
    finally:
        store.close()


def test_trigger_does_not_walk_past_one_hop(tmp_path):
    store = Store(tmp_path / 'hop.sqlite3', tmp_path)
    try:
        _topics(store, ('start', 1), ('bridge', 2), ('other', 3))
        _relate(store, 'bridge', 'start', 'follows_up')
        _relate(store, 'other', 'bridge', 'follows_up')
        store.set_episode_triggers('start', ['alpha'])
        slots = _slots(store, 'alpha')
        assert [item.attrib['id'] for item in slots] == ['bridge']
        assert [item.attrib['id'] for item in slots[0].findall('supplement')] == ['start']
        assert 'other' not in [item.get('id') for item in slots[0].iter('supplement')]
        assert slots[0].find('followed_by').attrib['id'] == 'other'
    finally:
        store.close()


def test_trigger_branches_ride_inside_newest_branch(tmp_path):
    store = Store(tmp_path / 'branch.sqlite3', tmp_path)
    try:
        _topics(store, ('root', 1), ('left', 2), ('right', 3))
        _relate(store, 'left', 'root', 'follows_up')
        _relate(store, 'right', 'root', 'follows_up')
        store.set_episode_triggers('root', ['alpha'])
        slots = _slots(store, 'alpha')
        assert [item.attrib['id'] for item in slots] == ['right']
        assert sorted(item.attrib['id'] for item in slots[0].findall('supplement')) == ['left', 'root']
    finally:
        store.close()


def test_trigger_revision_shows_latest_and_keeps_pointer(tmp_path):
    from momoi.runtime.context.triggers import attach_reply_episode_triggers
    store = Store(tmp_path / 'revise.sqlite3', tmp_path)
    try:
        _topics(store, ('plan', 1), ('change', 2), ('x0', 3), ('x1', 4), ('x2', 5), ('x3', 6))
        _relate(store, 'change', 'plan', 'revises')
        store.set_episode_triggers('plan', ['alpha'])
        slots = _slots(store, 'alpha')
        assert [item.attrib['id'] for item in slots] == ['change']
        assert slots[0].find('revises').attrib['id'] == 'plan'
        assert slots[0].find('supplement') is None
        for identifier in ('x0', 'x1', 'x2', 'x3'):
            store.set_episode_triggers(identifier, ['alpha'])
        assert [item.attrib['id'] for item in _slots(store, 'alpha')] == ['x3', 'x2', 'x1', 'x0', 'change']
        seen = set()
        result = {'bubbles': ['alpha']}
        attach_reply_episode_triggers(store, result, stage='heartbeat', seen=seen)
        assert seen == {'x3', 'x2', 'x1', 'x0', 'change'}
    finally:
        store.close()
