import asyncio
import unittest
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

from momoi.semantic.episode_reranker import EpisodeEvidenceReranker
from momoi.semantic.models import DenseRecallEvidence, EpisodeRerankMatch
from momoi.search import StringSearchBackend
from momoi.storage.episode_ranking import EpisodeRecallQuery, rank_episode_matches
from momoi.storage.episode_search import EpisodeQueryService, StringEpisodeSearchBackend
from tests.test_episode_search import document


class EpisodeRerankerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.request = {'queries': [{'query': '找旧事', 'candidates': [
            {'episode_id': 'a', 'evidence': [{'message_id': 1}]},
        ]}]}

    def test_rejects_foreign_citations_and_incomplete_queries(self):
        for outputs in ([], [{'query_index': 0, 'matches': [
            {'candidate_index': 0, 'evidence_indices': [2]}]}],
            [{'query_index': 0, 'matches': [
                {'candidate_index': 1, 'evidence_indices': [0]}]}]):
            with self.assertRaises(ValueError):
                EpisodeEvidenceReranker.parse({'queries': outputs}, self.request)

    def test_local_indices_map_to_owned_storage_ids(self):
        response = {"queries": [{"query_index": 0, "matches": [
            {"candidate_index": 0, "evidence_indices": [0]}]}]}
        self.assertEqual(EpisodeEvidenceReranker.parse(response, self.request),
                         {"找旧事": {"a": EpisodeRerankMatch(0, (1,))}})
        projected = EpisodeEvidenceReranker.model_input(self.request)
        candidate = projected["queries"][0]["candidates"][0]
        self.assertNotIn("episode_id", candidate)
        self.assertNotIn("message_id", candidate["evidence"][0])

    def test_valid_abstention_is_distinct_from_unreviewed(self):
        parsed = EpisodeEvidenceReranker.parse(
            {'queries': [{'query_index': 0, 'matches': []}]}, self.request)
        self.assertEqual(parsed, {'找旧事': {}})
        docs = [document('a', title='找旧事')]
        service = EpisodeQueryService(StringEpisodeSearchBackend(StringSearchBackend()))
        matches = service.match_many(['找旧事'], docs)
        self.assertTrue(rank_episode_matches([EpisodeRecallQuery('找旧事')], matches, docs))
        self.assertEqual(rank_episode_matches([EpisodeRecallQuery('找旧事')], matches,
            docs, dense_evidence=DenseRecallEvidence(reranked_episodes=parsed)), [])

    def test_semantic_selection_carries_cited_original_message(self):
        docs = [document('a', message='约好在图书馆碰面', role='user', delivery='delivered')]
        query = EpisodeRecallQuery('会合地点')
        service = EpisodeQueryService(StringEpisodeSearchBackend(StringSearchBackend()))
        cited = docs[0].messages[0].id
        dense = DenseRecallEvidence(reranked_episodes={query.dense_expression: {
            'a': EpisodeRerankMatch(0, (cited,))}})
        hits = rank_episode_matches([query], service.match_many([query.expression], docs),
                                    docs, dense_evidence=dense)
        self.assertEqual([m.id for m in hits[0].matches], [cited])
        self.assertIn('evidence_rerank', hits[0].channels)

    def test_time_window_excludes_unscoped_claims_and_cues(self):
        docs = [document("a", title="会合地点", message="今天在公园", role="user",
                         delivery="delivered", last_activity=100)]
        query = EpisodeRecallQuery("会合地点")
        store = SimpleNamespace(
            _episode_search_documents=Mock(return_value=({}, docs)),
            _episode_query=EpisodeQueryService(StringEpisodeSearchBackend(StringSearchBackend())),
            episode=Mock(return_value={"title": "会合地点", "updated_at": 100,
                "summarized_through_ordinal": 0, "recall_cues": ["昨天在图书馆"], "working_summary_claims": [
                    {"message_id": -1, "role": "user", "delivery_state": "delivered",
                     "quote": "昨天在图书馆"}]}),
        )
        reranker = EpisodeEvidenceReranker(store, Mock())
        request = reranker.request([query], DenseRecallEvidence(), after=50)
        store._episode_search_documents.assert_called_once_with(after=50, before=None)
        candidate = request["queries"][0]["candidates"][0]
        self.assertEqual(candidate["recall_cues"], [])
        self.assertNotIn(-1, [e["message_id"] for e in candidate["evidence"]])

    async def test_provider_failure_preserves_original_retrieval(self):
        provider = Mock()
        provider.complete = AsyncMock(side_effect=RuntimeError('unavailable'))
        reranker = EpisodeEvidenceReranker(None, lambda: provider)
        reranker.request = Mock(return_value=self.request)
        dense = DenseRecallEvidence(space_id='original')
        result = await reranker.rerank([], dense)
        self.assertEqual(result.space_id, 'original')
        self.assertEqual(result.reranked_episodes, {})
        self.assertEqual(result.rerank_fallback_reason, 'RuntimeError')

    async def test_missing_tool_is_repaired_once_within_same_request(self):
        provider = Mock()
        provider.complete = AsyncMock(side_effect=[
            SimpleNamespace(tool_calls=[]),
            SimpleNamespace(tool_calls=[SimpleNamespace(
                name="episode_evidence_select", arguments={"queries": [
                    {"query_index": 0, "matches": []}]} )]),
        ])
        reranker = EpisodeEvidenceReranker(None, lambda: provider)
        reranker.request = Mock(return_value=self.request)
        result = await reranker.rerank([], DenseRecallEvidence())
        self.assertEqual(result.rerank_attempts, 2)
        self.assertEqual(result.reranked_episodes, {"找旧事": {}})
        self.assertEqual(provider.complete.await_count, 2)

    async def test_cancellation_propagates(self):
        provider = Mock()
        provider.complete = AsyncMock(side_effect=asyncio.CancelledError())
        reranker = EpisodeEvidenceReranker(None, lambda: provider)
        reranker.request = Mock(return_value=self.request)
        with self.assertRaises(asyncio.CancelledError):
            await reranker.rerank([], DenseRecallEvidence())
