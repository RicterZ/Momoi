import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from momoi.storage import Store, encode_vector
from momoi.storage.episode_ranking import EpisodeRecallQuery
from momoi.semantic.service import SemanticRecallService
from momoi.integrations.models import EmbeddingConfig
from tests.test_semantic import vector


class TopicRecallTest(unittest.TestCase):
    def test_independent_cue_vectors_update_delete_and_aggregate_by_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'db')
            for id in ['a','b']:
                store.create_episode('旧话题', episode_id=id)
                store._db.execute("UPDATE conversation_episodes SET status='closed',recall_cues_json=? WHERE id=?",
                    (json.dumps([{'text': t, 'evidence_message_ids': [1]} for t in
                                 (['一起模拟面试','准备求职'] if id=='a' else ['职业规划'])]),id))
            store._db.commit()
            store.ensure_semantic_space(model='BAAI/bge-small-zh-v1.5',dimensions=512,
                calibration_profile='bge-small-zh-v1.5-momoi-v1',state='active')
            client=AsyncMock();client.encode.side_effect=lambda texts,query: [vector() for _ in texts]
            service=SemanticRecallService(store,EmbeddingConfig(enabled=True),client=client);service.start()
            async def run():
                await service.maintain_once()
                hits=await service.prepare([EpisodeRecallQuery('求职准备')],include_memory=False)
                self.assertEqual(set(hits.episodes['求职准备']),{'a','b'})
                self.assertEqual(hits.episodes['求职准备']['a'].cue_cosine,1.0)
                self.assertFalse(hasattr(service,'reranker'))
                store._db.execute("UPDATE conversation_episodes SET recall_cues_json='[]' WHERE id='a'");store._db.commit()
                await service.maintain_once()
                hits=await service.prepare([EpisodeRecallQuery('求职准备')],include_memory=False)
                self.assertIsNone(hits.episodes['求职准备']['a'].cue_cosine)
                self.assertEqual(hits.episodes['求职准备']['b'].cue_cosine,1.0)
            asyncio.run(run());store.close()

    def test_topic_query_uses_metadata_not_raw_claim_text(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db');store.create_episode('一次聊天',episode_id='a')
            store._db.execute("UPDATE conversation_episodes SET working_summary='只在原文的秘密词',narrative_summary='一起模拟面试',recall_cues_json=? WHERE id='a'",(json.dumps([{'text': '求职准备', 'evidence_message_ids': [1]}]),));store._db.commit()
            self.assertEqual(store.search_topic_queries([EpisodeRecallQuery('秘密词')],8),[])
            self.assertEqual(store.search_topic_queries([EpisodeRecallQuery('求职准备')],8)[0]['id'],'a')
            self.assertEqual(store.search_topic_queries([EpisodeRecallQuery('求职准备')],8)[0]['matches'],[])
            store.close()

    def test_template_upgrade_keeps_previous_encoder_space_available(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db')
            old=store.ensure_semantic_space(model='BAAI/bge-small-zh-v1.5',dimensions=512,
                calibration_profile='bge-small-zh-v1.5-momoi-v1',state='active')
            store._db.execute('UPDATE semantic_spaces SET document_template_version=3 WHERE id=?',(old['id'],));store._db.commit()
            service=SemanticRecallService(store,EmbeddingConfig(enabled=True),client=AsyncMock());service.start()
            self.assertEqual(service.degraded_reason,'');self.assertEqual(service.snapshot.space_id,old['id'])
            self.assertIsNotNone(store.semantic_space(state='building'));store.close()

    def test_legacy_vector_table_migration_preserves_ready_vectors(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'db';store=Store(path)
            space=store.ensure_semantic_space(model='BAAI/bge-small-zh-v1.5',dimensions=512,
                calibration_profile='bge-small-zh-v1.5-momoi-v1',state='active')
            db=store._db
            sql=db.execute("SELECT sql FROM sqlite_master WHERE name='semantic_documents'").fetchone()[0]
            with db:
                db.execute('DROP TABLE semantic_documents')
                db.execute(sql.replace(", 'episode_cue'",''))
                db.execute("""INSERT INTO semantic_documents
                    (space_id,document_type,source_id,content,content_sha256,state,vector,dimensions,created_at,updated_at)
                    VALUES (?,'episode_summary','a','text','hash','ready',?,512,1,1)""",
                    (space['id'],encode_vector(vector(),512)))
                db.execute('PRAGMA user_version=8')
            store.close()
            for _ in range(2):
                store=Store(path)
                row=store._db.execute("SELECT vector FROM semantic_documents WHERE source_id='a'").fetchone()
                self.assertEqual(row['vector'],encode_vector(vector(),512))
                sql=store._db.execute("SELECT sql FROM sqlite_master WHERE name='semantic_documents'").fetchone()[0]
                self.assertIn("'episode_cue'",sql)
                self.assertFalse(store._db.execute('PRAGMA foreign_key_check').fetchall())
                store.close()

    def test_reported_confidence_matches_hybrid_admission(self):
        from dataclasses import replace
        from momoi.semantic.models import DenseRecallEvidence, DenseEpisodeHit
        from momoi.storage.episode_ranking import rank_episode_matches
        from momoi.storage.episode_search import EpisodeSearchField, EpisodeQueryService, StringEpisodeSearchBackend
        from momoi.search import StringSearchBackend
        from tests.test_episode_search import document
        docs=[replace(document('a'),fields=(EpisodeSearchField('narrative_summary','邮箱'),),messages=())]
        query=EpisodeRecallQuery('邮箱')
        matcher=EpisodeQueryService(StringEpisodeSearchBackend(StringSearchBackend()))
        evidence=DenseRecallEvidence(calibration_profile='bge-small-zh-v1.5-momoi-v1',episodes={'邮箱':{'a':DenseEpisodeHit('a',summary_cosine=.60)}})
        hits=rank_episode_matches([query],matcher.match_many(['邮箱'],docs),docs,dense_evidence=evidence)
        self.assertEqual(len(hits),1)
        self.assertIn('hybrid',hits[0].admission_routes)
        self.assertNotIn('sparse',hits[0].admission_routes)
        self.assertGreaterEqual(hits[0].relevance_confidence,.47)

    def test_semantic_route_cannot_bypass_requested_confidence_floor(self):
        from dataclasses import replace
        from momoi.semantic.models import DenseRecallEvidence, DenseEpisodeHit
        from momoi.storage.episode_ranking import rank_episode_matches
        from momoi.storage.episode_search import EpisodeQueryService, StringEpisodeSearchBackend
        from momoi.search import StringSearchBackend
        from tests.test_episode_search import document
        docs=[replace(document('a'),fields=(),messages=())]
        matcher=EpisodeQueryService(StringEpisodeSearchBackend(StringSearchBackend()))
        evidence=DenseRecallEvidence(calibration_profile='bge-small-zh-v1.5-momoi-v1',episodes={'查询':{'a':DenseEpisodeHit('a',summary_cosine=.82)}})
        hits=rank_episode_matches([EpisodeRecallQuery('查询')],matcher.match_many(['查询'],docs),docs,dense_evidence=evidence,minimum_confidence=.99)
        self.assertEqual(hits,[])
