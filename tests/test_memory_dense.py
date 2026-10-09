"""Vector retrieval works with the memory tables and an injected encoder only."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import numpy as np
import pytest

from momoi.memory.retrieval.dense import DenseQueryService, DenseSearchPool, MemoryVectorRecall
from momoi.memory.retrieval.models import DenseThresholds
from momoi.memory.retrieval.service import MemoryRecallService
from momoi.memory.retrieval.snapshot import SegmentedVectorSnapshot
from momoi.memory.retrieval.vectors import encode_vector
from momoi.memory.storage.repository import MemoryRepository
from momoi.memory.storage.transactions import transaction
from momoi.memory.storage.vector_repository import VectorRepository
from tests.test_memory_repository import database, write


@pytest.fixture
def vectors(database):
    schema = (Path(__file__).parents[1] / 'src/momoi/storage/core/schema.sql').read_text()
    start = schema.index('CREATE TABLE IF NOT EXISTS semantic_documents (')
    database.execute(schema[start:schema.index(';', start) + 1])
    return VectorRepository(database)


def add_vector(db, source_id, vector=(1.0, 0.0), *, kind='confirmed_memory',
               chunk=0, parent='', starts_at=None, ends_at=None):
    db.execute(
        """INSERT INTO semantic_documents
           (space_id, document_type, source_id, parent_id, chunk_index, content,
            content_sha256, state, vector, dimensions, created_at, updated_at,
            starts_at, ends_at)
           VALUES ('space', ?, ?, ?, ?, 'test', 'hash', 'ready', ?, 2, 0, 0, ?, ?)""",
        (kind, str(source_id), parent, chunk, encode_vector(vector, 2), starts_at, ends_at),
    )
    db.commit()


def test_natural_search_encodes_and_reranks_without_preparing_evidence(database, vectors):
    repo = MemoryRepository(database)
    identifier = write(repo)
    add_vector(database, identifier)
    # A second chunk of the same memory must not duplicate a result.
    add_vector(database, identifier, chunk=1)
    snapshot = SegmentedVectorSnapshot(vectors, 2)
    snapshot.load('space')
    encoder = AsyncMock()
    encoder.encode.return_value = [[3.0, 0.0]]
    engine = DenseQueryService(snapshot, encoder, instruction='检索：')
    dense = MemoryVectorRecall(engine, {'confirmed_memory': DenseThresholds(.5, .7, .9)})
    rerank = AsyncMock(side_effect=lambda request, rows: rows)
    memory = MemoryRecallService(repo, dense_recall=dense, reranker=rerank)

    results = asyncio.run(memory.search('用户喜欢什么饮品'))

    assert [row['id'] for row in results] == [identifier]
    assert results[0]['channels'] == ['dense']
    assert results[0]['dense_cosine'] == pytest.approx(1.0)
    encoder.encode.assert_awaited_once_with(['检索：用户喜欢什么饮品'], query=True)
    rerank.assert_awaited_once()
    assert rerank.await_args.args[0] == '用户喜欢什么饮品'


def test_batch_shared_across_pools_and_time_scoped(database, vectors):
    add_vector(database, 'memory')
    add_vector(database, 'old', kind='episode_turn', parent='episode:old', starts_at=10, ends_at=20)
    add_vector(database, 'new', kind='episode_turn', parent='episode:new', starts_at=30, ends_at=40)
    snapshot = SegmentedVectorSnapshot(vectors, 2)
    snapshot.load('space')
    encoder = AsyncMock()
    encoder.encode.return_value = [[1.0, 0.0], [0.0, 1.0]]
    engine = DenseQueryService(snapshot, encoder)

    result = asyncio.run(engine.search(
        ['first', 'first', '', 'second'],
        [DenseSearchPool({'confirmed_memory'}), DenseSearchPool({'episode_turn'}, 25, 35)],
    ))

    encoder.encode.assert_awaited_once_with(['first', 'second'], query=True)
    assert result.query_batch_size == 2
    assert {meta.source_id for meta, score in result.hits['first']} == {'memory', 'new'}
    assert list(result.memory['first']) == [('confirmed_memory', 'memory')]


@pytest.mark.parametrize('response', [[], [[1.0]], [[float('nan'), 0]], [[0.0, 0.0]], TimeoutError('offline')])
def test_encoder_failure_preserves_sparse_recall(database, vectors, response):
    repo = MemoryRepository(database)
    identifier = write(repo)
    snapshot = SegmentedVectorSnapshot(vectors, 2)
    snapshot.load('space')
    encoder = AsyncMock()
    if isinstance(response, Exception):
        encoder.encode.side_effect = response
    else:
        encoder.encode.return_value = response
    failures = []
    engine = DenseQueryService(snapshot, encoder, on_failure=lambda error, count: failures.append((error, count)))
    memory = MemoryRecallService(repo, dense_recall=MemoryVectorRecall(engine, {}))

    results = asyncio.run(memory.search('无糖咖啡'))

    assert [row['id'] for row in results] == [identifier]
    assert results[0]['channels'] == ['sparse']
    assert len(failures) == 1 and failures[0][1] == 1


def test_snapshot_filters_other_pools_and_stale_generations_before_top_k(database, vectors):
    add_vector(database, 'memory', (.8, .6))
    add_vector(database, 'stale')
    add_vector(database, 'episode', kind='episode_summary')
    snapshot = SegmentedVectorSnapshot(vectors, 2)
    snapshot.load('space')
    database.execute("DELETE FROM semantic_documents WHERE source_id='stale'")
    database.commit()
    snapshot.replace_source('confirmed_memory', 'stale')

    hits = snapshot.search(np.array([[1.0, 0.0]]), {'confirmed_memory'}, 1)

    assert [meta.source_id for meta, score in hits[0]] == ['memory']
    assert hits[0][0][1] == pytest.approx(.8)


def test_invalid_vector_invalidation_preserves_outer_transaction_and_pagination(database, vectors):
    add_vector(database, 'a')
    add_vector(database, 'b')
    database.execute("UPDATE semantic_documents SET vector=x'00' WHERE source_id='a'")
    database.commit()
    pages = vectors.ready_documents('space', page_size=1)
    snapshot = SegmentedVectorSnapshot(vectors, 2)
    snapshot.space_id = 'space'
    with pytest.raises(RuntimeError, match='rollback'):
        with transaction(database):
            database.execute("INSERT INTO application_work VALUES ('outer')")
            snapshot._append(next(pages))
            assert database.in_transaction
            assert database.execute("SELECT state FROM semantic_documents WHERE source_id='a'").fetchone()[0] == 'retry'
            # Invalidating a must not make b disappear from the following page.
            snapshot._append(next(pages))
            assert list(pages) == []
            assert snapshot.search(np.array([[1.0, 0.0]]), {'confirmed_memory'}, 1)[0][0][0].source_id == 'b'
            raise RuntimeError('rollback')
    assert database.execute('SELECT COUNT(*) FROM application_work').fetchone()[0] == 0
    assert database.execute("SELECT state FROM semantic_documents WHERE source_id='a'").fetchone()[0] == 'ready'


def test_unavailable_empty_and_cancelled_queries_do_not_encode_or_hide_cancellation(database, vectors):
    snapshot = SegmentedVectorSnapshot(vectors, 2)
    encoder = AsyncMock()
    engine = DenseQueryService(snapshot, encoder)
    pools = [DenseSearchPool({'confirmed_memory'})]
    assert asyncio.run(engine.search(['x'], pools)).fallback_reason == 'no_active_space'
    snapshot.load('space')
    engine.unavailable_reason = 'disabled'
    assert asyncio.run(engine.search(['x'], pools)).fallback_reason == 'disabled'
    assert asyncio.run(engine.search([], pools)).query_batch_size == 0
    encoder.encode.assert_not_awaited()
    engine.unavailable_reason = ''
    encoder.encode.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(engine.search(['x'], pools))


def test_parent_grouping_and_replacement_leave_other_sources_available(database, vectors):
    add_vector(database, 'one', kind='episode_turn', parent='parent:a')
    add_vector(database, 'two', kind='episode_turn', parent='parent:a')
    add_vector(database, 'three', (.8, .6), kind='episode_turn', parent='parent:b')
    add_vector(database, 'parent:a', kind='episode_summary')
    snapshot = SegmentedVectorSnapshot(vectors, 2)
    snapshot.load('space')
    hits = snapshot.search(
        np.array([[1.0, 0.0]]), {'episode_turn'}, 2, group_by_parent=True,
    )[0]
    assert [meta.parent_id for meta, _ in hits] == ['parent:a', 'parent:b']
    database.execute("DELETE FROM semantic_documents WHERE parent_id='parent:a' OR source_id='parent:a'")
    database.commit()
    snapshot.replace_source('episode_summary', 'parent:a', include_children=True)
    hits = snapshot.search(
        np.array([[1.0, 0.0]]), {'episode_turn', 'episode_summary'}, 3, group_by_parent=True,
    )[0]
    assert [meta.source_id for meta, _ in hits] == ['three']
