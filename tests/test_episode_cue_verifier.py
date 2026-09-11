import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from momoi.semantic.episode_cue_verifier import verify_episode_cues


class EpisodeCueVerifierTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_admitted_cues_survive_without_rewriting(self):
        cues = [{'text': text, 'evidence_message_ids': [17]}
                for text in ['OWNER 说很累', 'OWNER 已经入睡']]
        provider = SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace(
            tool_calls=[SimpleNamespace(name='episode_cue_admit',
                                        arguments={'supported_indices': [0]})])))
        result = await verify_episode_cues(provider, cues, [
            {'message_id': 17, 'role': 'user', 'quote': '很累'}])
        self.assertEqual(result, cues[:1])
        self.assertEqual(len(cues), 2)

    async def test_invalid_source_link_never_reaches_model(self):
        provider = SimpleNamespace(complete=AsyncMock())
        with self.assertRaises(ValueError):
            await verify_episode_cues(provider, [
                {'text': 'unknown', 'evidence_message_ids': [99]}],
                [{'message_id': 17}])
        provider.complete.assert_not_called()

    async def test_invalid_verdict_cannot_admit_cues(self):
        for indices in ([True], [2], [0, 0], None):
            provider = SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace(
                tool_calls=[SimpleNamespace(name='episode_cue_admit',
                    arguments={'supported_indices': indices})])))
            with self.assertRaises(ValueError):
                await verify_episode_cues(provider, [
                    {'text': 'OWNER 说很累', 'evidence_message_ids': [17]}],
                    [{'message_id': 17}])


def test_query_cue_can_reference_all_messages_needed_for_its_retrieval_intent():
    from momoi.storage.episode.episode_cues import normalize_cues

    # One retrieval intent can span more messages than the number of cues.
    claims = [{"message_id": i} for i in range(1, 11)]
    cues = [{"text": "回顾多轮修改后的最终约定时", "evidence_message_ids": list(range(1, 11))}]
    assert normalize_cues(cues, claims) == cues
    import pytest
    with pytest.raises(ValueError, match="retained verified claims"):
        normalize_cues(cues, claims[:-1])
