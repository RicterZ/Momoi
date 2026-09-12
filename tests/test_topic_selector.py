import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree import ElementTree

from momoi.integrations.request_context import requested_thinking_effort
from momoi.models import ProviderResponse, ToolCall
from momoi.semantic.topic_selector import RecallSelection, select_topics
from momoi.storage.episode.episode_ranking import EpisodeRecallQuery


def response(indices, memory_indices=(), reflection_indices=()):
    return ProviderResponse([], [ToolCall('selection', 'select_topics', {
        'indices': indices,
        'memory_indices': list(memory_indices),
        'reflection_indices': list(reflection_indices),
    })])


def test_topic_selection_preserves_all_eight_and_model_order_without_evidence():
    rows = [dict(id=str(i), title='topic', narrative_summary='summary', topics=['topic'],
                 recall_cues=[{'text': 'cue', 'evidence_message_ids': [123]}],
                 working_summary='PRIVATE RAW TEXT', matches=['PRIVATE RAW TEXT']) for i in range(8)]
    store = SimpleNamespace(topic_conversation_time=lambda _: {'start': 'start', 'end': 'end'})

    async def complete(system, messages, tools, **kwargs):
        assert requested_thinking_effort() == 'low'
        payload = ElementTree.fromstring(messages[0]['content'])
        assert 'PRIVATE RAW TEXT' not in messages[0]['content']
        candidate = payload.find('candidates/candidate')
        assert payload.findtext('current_request') == 'request'
        assert payload.findtext('retrieval_queries/query/semantic') == 'topic'
        assert [node.text for node in candidate.findall('cues/cue')] == ['cue']
        assert candidate.find('conversation_time').attrib == {'start': 'start', 'end': 'end'}
        assert candidate.find('updated_at') is None
        return response(list(reversed(range(8))))

    provider = SimpleNamespace(complete=complete)
    selected = asyncio.run(select_topics(provider, store, 'request', [EpisodeRecallQuery('topic')], rows))
    assert selected == RecallSelection(list(reversed(rows)), [], [])


def test_invalid_indices_repair_and_fail_closed():
    rows = [dict(id='a', title='topic')]
    store = SimpleNamespace(topic_conversation_time=lambda _: None)
    provider = SimpleNamespace(complete=AsyncMock(side_effect=[response([True]), response([0])]))
    assert asyncio.run(select_topics(provider, store, 'request', [], rows)) == RecallSelection(rows, [], [])
    assert provider.complete.await_count == 2
    provider.complete = AsyncMock(return_value=response([9]))
    assert asyncio.run(select_topics(provider, store, 'request', [], rows)) == RecallSelection([], [], [])
    assert provider.complete.await_count == 2
    provider.complete = AsyncMock(return_value=response([]))
    assert asyncio.run(select_topics(provider, store, 'request', [], rows)) == RecallSelection([], [], [])
    provider.complete.assert_awaited_once()


def test_runtime_prefilter_bypasses_gate_and_keeps_selection_order(tmp_path):
    from momoi.runtime.context.service import ContextService
    from momoi.runtime.context.retrieval import build_plan_retrieval, select_plan_recall_queries
    from momoi.runtime.context.rendering import assemble_main_context
    from momoi.storage import Store
    from tests.test_context_assembler import config

    store = Store(tmp_path / 'db')
    try:
        for i in range(9):
            store.create_episode('shared topic', episode_id=str(i))
            store._db.execute("UPDATE conversation_episodes SET narrative_summary='shared topic' WHERE id=?", (str(i),))
        store._db.commit()
        service = ContextService()
        service.store = store
        service.config = config(str(tmp_path), summary_results=3)
        service.config.thinking_stages["topic_selection"] = "medium"
        plan = {'version': 7, 'intent_units': [{'id': 'u', 'recall_queries': [
            {'semantic': 'shared topic', 'keywords': ['shared']}]}], 'episode_actions': []}
        queries, *_ = select_plan_recall_queries(plan)
        captured = []

        async def complete(system, messages, tools, **kwargs):
            payload = ElementTree.fromstring(messages[0]['content'])
            assert requested_thinking_effort() == "medium"
            captured.append(payload)
            return response(list(reversed(range(len(payload.findall('candidates/candidate'))))))

        service.provider = SimpleNamespace(complete=complete)
        selected = asyncio.run(service._select_recall_topics('shared topic', queries, None))
        assert len(captured[0].findall('candidates/candidate')) == len(selected.episodes) == 8
        retrieval = build_plan_retrieval(store, plan, service.config, selected_episode_rows=selected.episodes)
        assert [r['episode_id'] for r in retrieval['episodes']] == [r['id'] for r in selected.episodes]
        rendered = assemble_main_context(store, retrieval, 8000)['episodes']
        assert rendered.count('<episode ') == 8
        empty = build_plan_retrieval(store, plan, service.config, selected_episode_rows=[])
        assert empty['episodes'] == []
    finally:
        store.close()


def test_configured_effort_applies_to_initial_call_and_repair():
    from momoi.integrations.request_context import model_request
    from momoi.observability.context import current_log_context, log_context
    rows = [dict(id='a', title='topic')]
    store = SimpleNamespace(topic_conversation_time=lambda _: None)
    for effort in ['low', 'medium', 'high', 'xhigh', 'max', '']:
        seen = []
        call_contexts = []

        async def complete(*args, **kwargs):
            assert current_log_context()['stage'] == 'topic_selection'
            call_contexts.append(current_log_context())
            seen.append(requested_thinking_effort('provider-default'))
            return response([9] if len(seen) == 1 else [0])

        async def run():
            with log_context(turn_id='turn-one', call_id='owner-call', round=3):
                with model_request(thinking_effort='high'):
                    result = await select_topics(SimpleNamespace(complete=complete), store,
                                                 'request', [], rows, thinking_effort=effort)
                    assert requested_thinking_effort() == 'high'
                    return result

        assert asyncio.run(run()) == RecallSelection(rows, [], [])
        assert seen == [effort or 'provider-default'] * 2
        assert [context['turn_id'] for context in call_contexts] == ['turn-one'] * 2
        assert all(context['call_id'] != 'owner-call' for context in call_contexts)
        assert len({context['call_id'] for context in call_contexts}) == 2


def test_topic_selection_filters_memory_and_reflection_candidates_in_returned_order():
    episodes = [dict(id='episode', title='episode')]
    memories = [
        dict(id=1, source='confirmed', kind='profile', key='teacher.lunch', content='12:20 一起吃午饭。'),
        dict(id=2, source='confirmed', kind='practice', key='motorcycle.tools', content='带上套筒扳手。'),
        dict(id=3, source='reflection', kind='preference', key='food.place', content='主人常去观音桥吃饭。',
             local_date='2026-09-11', confidence=0.8, evidence='主人提到观音桥。'),
    ]
    store = SimpleNamespace(topic_conversation_time=lambda _: None)

    async def complete(_system, messages, _tools, **_kwargs):
        payload = ElementTree.fromstring(messages[0]['content'])
        assert [node.findtext('key') for node in payload.findall('memory_candidates/candidate')] == [
            'teacher.lunch', 'motorcycle.tools'
        ]
        assert [node.findtext('key') for node in payload.findall('reflection_candidates/candidate')] == ['food.place']
        return response([], [1, 0], [])

    selected = asyncio.run(select_topics(
        SimpleNamespace(complete=complete), store, '修车后在哪里吃午饭', [], episodes,
        memory_candidates=memories,
    ))
    assert selected == RecallSelection([], memories[:2][::-1], [])
