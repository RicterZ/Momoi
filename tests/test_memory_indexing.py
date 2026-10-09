"""Index persistence and maintenance without Momoi Store or runtime tables."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from momoi.memory.indexing.worker import IndexAdapter, IndexWorker
from momoi.memory.retrieval.dense import DenseQueryService, MemoryVectorRecall
from momoi.memory.retrieval.models import DenseThresholds
from momoi.memory import Memory
from momoi.memory.retrieval.snapshot import SegmentedVectorSnapshot
from momoi.memory.storage.index_documents import IndexDocuments
from momoi.memory.storage.index_queue import IndexQueue
from momoi.memory.storage.index_records import IndexDocument
from momoi.memory.storage.repository import MemoryRepository
from momoi.memory.storage.transactions import transaction
from momoi.memory.storage.vector_repository import VectorRepository
from tests.test_memory_repository import database, write


@pytest.fixture
def index(database):
    schema = (Path(__file__).parents[1] / 'src/momoi/storage/core/schema.sql').read_text()
    for table in ('semantic_spaces', 'semantic_dirty_sources', 'semantic_documents'):
        start = schema.index(f'CREATE TABLE IF NOT EXISTS {table} (')
        database.execute(schema[start:schema.index(';', start) + 1])
    with transaction(database):
        database.execute(
            """INSERT INTO semantic_spaces
               (id, provider, model, dimensions, query_template_version,
                document_template_version, calibration_profile, state, created_at)
               VALUES ('space', 'test', 'fake', 2, 1, 1, 'test', 'building', 0)""",
        )
    return IndexQueue(database), IndexDocuments(database)


def enqueue(db, source_id='1', *, stamp=1.0, kind='confirmed_memory'):
    with transaction(db):
        db.execute(
            """INSERT INTO semantic_dirty_sources (source_type, source_id, changed_at)
               VALUES (?, ?, ?) ON CONFLICT(source_type, source_id) DO UPDATE
               SET changed_at=excluded.changed_at, claimed_at=NULL, retry_at=NULL""",
            (kind, source_id, stamp),
        )


def document(text='喜欢无糖咖啡', *, source_id='1', chunk=0):
    return IndexDocument('confirmed_memory', source_id, '', chunk, text)


def materialize(db, index, documents, *, stamp=1):
    queue, repository = index
    enqueue(db, documents[0].source_id, stamp=stamp)
    claim = queue.claim_sources()[0]
    return repository.materialize(claim, documents, document_type='confirmed_memory')


def adapter_for(index, documents):
    queue, repository = index
    adapter = Mock(spec=IndexAdapter)
    adapter.materialize.side_effect = lambda claim: repository.materialize(
        claim, documents, document_type='confirmed_memory',
    )
    adapter.spaces.return_value = [{'id': 'space', 'dimensions': 2}]
    adapter.source_key.side_effect = lambda row: (str(row['document_type']), str(row['source_id']))
    return adapter


def test_index_worker_builds_searchable_memory_without_application(database, index):
    queue, documents = index
    identifier = write(MemoryRepository(database))
    enqueue(database, str(identifier))
    encoder = AsyncMock()
    encoder.encode.return_value = [[1.0, 0.0]]
    snapshot = SegmentedVectorSnapshot(VectorRepository(database), 2)
    adapter = adapter_for(index, [document(source_id=str(identifier))])
    adapter.activate_ready.side_effect = lambda: snapshot.load('space')
    worker = IndexWorker(queue, encoder, adapter, document_batch_size=8)
    memory = Memory(
        database, dense_recall=MemoryVectorRecall(
            DenseQueryService(snapshot, encoder),
            {'confirmed_memory': DenseThresholds(.5, .7, .9)},
        ),
    )

    async def run():
        assert await worker.maintain_once(allow_encoding=False)
        encoder.encode.assert_not_awaited()
        adapter.activate_ready.assert_not_called()
        assert await worker.maintain_once()
        return await memory.search('用户喜欢什么饮品')

    adapter.materialize.side_effect = lambda claim: documents.materialize(
        claim, memory.index_source.documents(str(claim['source_id'])),
        document_type='confirmed_memory',
    )
    results = asyncio.run(run())
    assert [row['id'] for row in results] == [identifier]
    assert results[0]['channels'] == ['dense']
    assert [call.kwargs['query'] for call in encoder.encode.await_args_list] == [False, True]
    assert not database.in_transaction
    adapter.report.assert_not_called()


def test_claim_and_materialization_never_commit_callers_work(database, index):
    queue, repository = index
    enqueue(database)
    with pytest.raises(RuntimeError, match='rollback'):
        with transaction(database):
            database.execute("INSERT INTO application_work VALUES ('outer')")
            claim = queue.claim_sources()[0]
            assert repository.materialize(claim, [document()], document_type='confirmed_memory') == 1
            assert database.in_transaction
            assert queue.claim_documents('space', 1)
            raise RuntimeError('rollback')
    assert database.execute('SELECT COUNT(*) FROM semantic_documents').fetchone()[0] == 0
    assert database.execute('SELECT COUNT(*) FROM application_work').fetchone()[0] == 0
    assert len(queue.claim_sources()) == 1


def test_invalid_vector_batch_rolls_back_without_discarding_callers_work(database, index):
    queue, _ = index
    materialize(database, index, [document(), document(chunk=1)])
    rows = queue.claim_documents('space', 8)
    with transaction(database):
        database.execute("INSERT INTO application_work VALUES ('outer')")
        with pytest.raises(ValueError, match='dimension'):
            queue.finish_documents(rows, [[1, 0], [1]], 2, [('confirmed_memory', '1')] * 2)
        assert database.in_transaction
        assert database.execute('SELECT COUNT(*) FROM application_work').fetchone()[0] == 1
        assert {row[0] for row in database.execute('SELECT state FROM semantic_documents')} == {'encoding'}
        assert database.execute('SELECT COUNT(*) FROM semantic_documents WHERE vector IS NOT NULL').fetchone()[0] == 0
        queue.fail_documents(rows, TimeoutError('offline'))
        assert {row[0] for row in database.execute('SELECT state FROM semantic_documents')} == {'retry'}
    assert not database.in_transaction
    assert queue.claim_documents('space', 8) == []


def test_stale_source_claim_cannot_overwrite_new_documents_or_retry_new_claim(database, index):
    queue, repository = index
    enqueue(database, stamp=1)
    old = queue.claim_sources()[0]
    enqueue(database, stamp=2)
    current = queue.claim_sources()[0]
    assert repository.materialize(old, [document('old')], document_type='confirmed_memory') == 0
    queue.fail_source(old, ValueError('late error'))
    pending = database.execute('SELECT * FROM semantic_dirty_sources').fetchone()
    assert pending['changed_at'] == 2 and pending['claimed_at'] is not None
    assert pending['last_error'] is None
    repository.materialize(current, [document('new')], document_type='confirmed_memory')
    queue.fail_source(old, ValueError('already gone'))
    assert database.execute('SELECT content FROM semantic_documents').fetchone()[0] == 'new'


def test_dirty_source_and_changed_content_reject_late_embeddings(database, index):
    queue, repository = index
    materialize(database, index, [document('old')])
    old = queue.claim_documents('space', 1)
    enqueue(database, stamp=2)
    assert queue.finish_documents(old, [[1, 0]], 2, [('confirmed_memory', '1')]) == []
    repository.materialize(queue.claim_sources()[0], [document('new')], document_type='confirmed_memory')
    new = queue.claim_documents('space', 1)
    assert queue.finish_documents(old, [[1, 0]], 2, [('confirmed_memory', '1')]) == []
    assert queue.finish_documents(new, [[0, 1]], 2, [('confirmed_memory', '1')])
    blob = database.execute('SELECT vector FROM semantic_documents').fetchone()[0]
    assert materialize(database, index, [document('new')], stamp=3) == 0
    assert database.execute('SELECT vector FROM semantic_documents').fetchone()[0] == blob
    assert queue.claim_documents('space', 1) == []


def test_worker_reports_encoding_failure_and_retries_after_backoff(database, index):
    queue, _ = index
    enqueue(database)
    adapter = adapter_for(index, [document()])
    encoder = AsyncMock()
    encoder.encode.side_effect = TimeoutError('offline')
    worker = IndexWorker(queue, encoder, adapter, document_batch_size=4)
    assert asyncio.run(worker.maintain_once())
    row = database.execute('SELECT * FROM semantic_documents').fetchone()
    assert row['state'] == 'retry' and row['last_error'] == 'TimeoutError: offline'
    assert queue.claim_documents('space', 1) == []
    assert adapter.report.call_args.args[0] == 'encode_failed'
    with transaction(database):
        database.execute('UPDATE semantic_documents SET retry_at=0')
    encoder.encode.side_effect = None
    encoder.encode.return_value = [[1, 0]]
    assert asyncio.run(worker.maintain_once())
    assert database.execute('SELECT state FROM semantic_documents').fetchone()[0] == 'ready'


def test_worker_source_failure_is_requeued_and_cancelled_encoding_can_recover(database, index):
    queue, _ = index
    enqueue(database)
    adapter = adapter_for(index, [document()])
    original = adapter.materialize.side_effect
    adapter.materialize.side_effect = ValueError('bad source')
    encoder = AsyncMock()
    worker = IndexWorker(queue, encoder, adapter, document_batch_size=4)
    assert not asyncio.run(worker.maintain_once())
    pending = database.execute('SELECT * FROM semantic_dirty_sources').fetchone()
    assert pending['claimed_at'] is None and pending['retry_at'] is not None
    assert queue.claim_sources() == []
    assert adapter.report.call_args.args[0] == 'materialize_failed'
    with transaction(database):
        database.execute('UPDATE semantic_dirty_sources SET retry_at=0')
    adapter.materialize.side_effect = original
    encoder.encode.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.maintain_once())
    assert queue.recover_encoding() == 1
    assert len(queue.claim_documents('space', 4)) == 1


def test_parent_documents_can_be_deactivated_reused_and_deleted(database, index):
    queue, repository = index
    doc = IndexDocument('episode_summary', 'parent', 'parent', 0, 'summary')

    def replace(documents, *, stamp, keep):
        enqueue(database, 'parent', stamp=stamp, kind='episode')
        return repository.materialize(
            queue.claim_sources()[0], documents, document_type='episode_summary',
            include_children=True, keep_removed=keep,
        )

    replace([doc], stamp=1, keep=True)
    rows = queue.claim_documents('space', 4)
    queue.finish_documents(rows, [[1, 0]], 2, [('episode', 'parent')])
    blob = database.execute('SELECT vector FROM semantic_documents').fetchone()[0]
    replace([], stamp=2, keep=True)
    assert database.execute('SELECT state FROM semantic_documents').fetchone()[0] == 'inactive'
    replace([doc], stamp=3, keep=True)
    row = database.execute('SELECT state, vector FROM semantic_documents').fetchone()
    assert row['state'] == 'ready' and row['vector'] == blob
    assert queue.claim_documents('space', 4) == []
    replace([], stamp=4, keep=False)
    assert database.execute('SELECT COUNT(*) FROM semantic_documents').fetchone()[0] == 0
