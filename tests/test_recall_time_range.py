import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator

from momoi.models import IncomingMessage
from momoi.runtime import MomoiDaemon
from momoi.runtime.context.retrieval import recall_episode_window, select_plan_recall_queries
from momoi.runtime.tool_contracts.context import RECALL_TOOL_SPEC, recall_search_arguments
from momoi.storage.episode.episode_ranking import EpisodeRecallQuery
from tests.support import config
from tests.test_memory_tool_filters import seed

DAY = {'kind': 'range', 'from': '2026-11-11T00:00', 'to': '2026-11-12T00:00'}
INSIDE = datetime.fromisoformat('2026-11-11T20:00').timestamp()
OUTSIDE = datetime.fromisoformat('2026-11-05T20:00').timestamp()


@pytest.fixture
def daemon(tmp_path):
    d = MomoiDaemon(config(str(tmp_path)))
    yield d
    d.store.close()


def episode(store, episode_id, title, created_at):
    store.create_episode(title, episode_id=episode_id)
    store.begin_turn(f'turn-{episode_id}', 'owner', [])
    store._db.execute("UPDATE conversation_episodes SET status='closed', narrative_summary=? WHERE id=?",
                      (title, episode_id))
    store._db.execute('INSERT INTO episode_turns(episode_id,turn_id,ordinal,relation) VALUES(?,?,1,?)',
                      (episode_id, f'turn-{episode_id}', 'primary'))
    store._db.execute('INSERT INTO messages(turn_id,role,content,created_at,source_event_ids_json) VALUES(?,?,?,?,?)',
                      (f'turn-{episode_id}', 'user', title, created_at, '[]'))
    store._db.commit()


def test_schema_accepts_time_range_alone_and_normalizes_window():
    validator = Draft202012Validator(RECALL_TOOL_SPEC['input_schema'])
    assert validator.is_valid({'time_range': DAY})
    assert validator.is_valid({'semantic': ['老师下班'], 'time_range': {'kind': 'recent', 'days': 1}})
    assert not validator.is_valid({'time_range': {'kind': 'yesterday'}})
    assert recall_search_arguments({'time_range': DAY})['time_range'] == DAY
    assert recall_search_arguments({'semantic': ['老师下班']})['time_range'] is None
    assert recall_search_arguments({'semantic': ['老师下班'], 'time_range': {'kind': 'all'}})['time_range'] is None
    for invalid in ({'time_range': {'kind': 'all'}},
                    {'time_range': {'kind': 'range', 'from': '2026-11-12', 'to': '2026-11-11'}},
                    {'time_range': {'kind': 'range', 'from': 'today'}}):
        with pytest.raises(ValueError):
            recall_search_arguments(invalid)


def test_plan_carries_window_to_every_query_without_touching_memory_filters(daemon):
    plan = daemon._plan_from_submission([], {'semantic': ['老师下班', '老师晚饭'], 'time_range': DAY},
                                        turn_id='t', revision=1)
    selected, *_ = select_plan_recall_queries(plan)
    assert [item['episode_time_range'] for item in selected] == [DAY, DAY]
    assert all(item.get('memory_filters', {}).get('tags_any') == [] for item in selected)
    after, before = recall_episode_window(selected)
    assert after == datetime.fromisoformat('2026-11-11T00:00').timestamp()
    assert before == datetime.fromisoformat('2026-11-12T00:00').timestamp()
    assert recall_episode_window([{'semantic_expression': 'x'}]) == (None, None)


def test_topic_search_keeps_only_episodes_with_messages_in_window(daemon):
    store = daemon.store
    episode(store, 'today', '老师下班后在路边叫车回家', INSIDE)
    episode(store, 'old', '老师下班后在路边叫车回家', OUTSIDE)
    query = [EpisodeRecallQuery('叫车')]
    assert {row['id'] for row in store.search_topic_queries(query, 5, minimum_confidence=0)} == {'today', 'old'}
    after, before = recall_episode_window([{'episode_time_range': DAY}])
    hits = store.search_topic_queries(query, 5, minimum_confidence=0, after=after, before=before)
    assert [row['id'] for row in hits] == ['today']


def test_time_only_owner_recall_browses_window_without_models(daemon):
    store = daemon.store
    episode(store, 'today', '老师今天早上七点出门上班', INSIDE)
    episode(store, 'old', '老师上周的早安', OUTSIDE)
    seed(daemon.memory, 'unrelated', ['health'])
    daemon.semantic_recall.prepare = AsyncMock(side_effect=AssertionError('no embeddings for time browsing'))
    event = IncomingMessage('time-event', 'owner', '今天发生了什么', 10, 10)
    store.add_event(event)
    store.begin_turn('time-turn', 'owner', [event.event_id])
    asyncio.run(daemon.submit_owner_context([event], 'time-turn', {'time_range': DAY}))
    retrieval = store.context_plan('time-turn')['retrieval']
    assert [item['episode_id'] for item in retrieval['episodes']] == ['today']
    assert retrieval['episodes'][0]['unit_ids'] == ['u1']
    assert retrieval['recall_memories'] == []
    daemon.semantic_recall.prepare.assert_not_awaited()


def test_heartbeat_recall_applies_same_window(daemon):
    store = daemon.store
    episode(store, 'today', '老师今天早上七点出门上班', INSIDE)
    episode(store, 'old', '老师上周的早安', OUTSIDE)
    result = asyncio.run(daemon.prepare_heartbeat_context({
        'activity': '记忆筛选', 'mode': 'work', 'strategy': ['查看今天'],
        'recall_mode': 'search', 'recall_queries': [{'semantic': '', 'keywords': []}],
        'memory_filters': {}, 'episode_time_range': DAY,
    }))
    assert result['plan']['activity']['episode_time_range'] == DAY
    assert '老师今天早上七点出门上班' in str(result['context'])
    assert '老师上周的早安' not in str(result['context'])
