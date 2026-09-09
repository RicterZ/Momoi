import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from momoi.integrations.request_context import requested_thinking_effort
from momoi.models import ProviderResponse, ToolCall
from momoi.semantic.topic_selector import select_topics
from momoi.storage.episode_ranking import EpisodeRecallQuery


def response(indices):
    return ProviderResponse([], [ToolCall('selection', 'select_topics', {'indices': indices})])


def test_topic_selection_preserves_all_eight_and_model_order_without_evidence():
    rows = [dict(id=str(i), title='topic', narrative_summary='summary', topics=['topic'],
                 recall_cues=[{'text': 'cue', 'evidence_message_ids': [123]}],
                 working_summary='PRIVATE RAW TEXT', matches=['PRIVATE RAW TEXT']) for i in range(8)]
    store = SimpleNamespace(topic_conversation_time=lambda _: {'start': 'start', 'end': 'end'})

    async def complete(system, messages, tools, **kwargs):
        assert requested_thinking_effort() == 'low'
        payload = json.loads(messages[0]['content'])
        assert 'PRIVATE RAW TEXT' not in messages[0]['content']
        assert payload['candidates'][0]['cues'] == ['cue']
        assert payload['candidates'][0]['conversation_time']['start'] == 'start'
        assert 'updated_at' not in payload['candidates'][0]
        return response(list(reversed(range(8))))

    provider = SimpleNamespace(complete=complete)
    selected = asyncio.run(select_topics(provider, store, 'request', [EpisodeRecallQuery('topic')], rows))
    assert selected == list(reversed(rows))


def test_invalid_indices_repair_and_fail_closed():
    rows = [dict(id='a', title='topic')]
    store = SimpleNamespace(topic_conversation_time=lambda _: None)
    provider = SimpleNamespace(complete=AsyncMock(side_effect=[response([True]), response([0])]))
    assert asyncio.run(select_topics(provider, store, 'request', [], rows)) == rows
    assert provider.complete.await_count == 2
    provider.complete = AsyncMock(return_value=response([9]))
    assert asyncio.run(select_topics(provider, store, 'request', [], rows)) == []
    assert provider.complete.await_count == 2
    provider.complete = AsyncMock(return_value=response([]))
    assert asyncio.run(select_topics(provider, store, 'request', [], rows)) == []
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
        plan = {'version': 7, 'intent_units': [{'id': 'u', 'recall_queries': [
            {'semantic': 'shared topic', 'keywords': ['shared']}]}], 'episode_actions': []}
        queries, *_ = select_plan_recall_queries(plan)
        captured = []

        async def complete(system, messages, tools, **kwargs):
            payload = json.loads(messages[0]['content'])
            captured.append(payload)
            return response(list(reversed(range(len(payload['candidates'])))))

        service.provider = SimpleNamespace(complete=complete)
        selected = asyncio.run(service._select_recall_topics('shared topic', queries, None))
        assert len(captured[0]['candidates']) == len(selected) == 8
        retrieval = build_plan_retrieval(store, plan, service.config, selected_episode_rows=selected)
        assert [r['episode_id'] for r in retrieval['episodes']] == [r['id'] for r in selected]
        rendered = assemble_main_context(store, retrieval, 8000)['episodes']
        assert rendered.count('<episode ') == 8
        empty = build_plan_retrieval(store, plan, service.config, selected_episode_rows=[])
        assert empty['episodes'] == []
    finally:
        store.close()
