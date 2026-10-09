import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from momoi.memory import Memory, MemoryRecallQuery
from momoi.memory.retrieval.models import DenseMemoryHit, DenseThresholds
from momoi.memory.retrieval.dense import VectorMemoryEvidence
from momoi.memory.storage.transactions import transaction
from tests.test_memory_repository import database, write


def test_public_memory_api_shares_search_snapshot_and_index_source(database):
    dense = AsyncMock()
    rerank = AsyncMock(side_effect=lambda request, rows: rows)
    memory = Memory(database, dense_recall=dense, reranker=rerank)
    identifier = write(memory.repository)
    query = '用户喜欢什么饮品'
    dense.return_value = VectorMemoryEvidence(
        {query: {('confirmed_memory', str(identifier)): DenseMemoryHit(str(identifier), 1.0)}},
        {'confirmed_memory': DenseThresholds(.5, .7, .9)},
    )

    results = asyncio.run(memory.search(query))

    assert [row['id'] for row in results] == [identifier]
    assert results[0]['channels'] == ['dense']
    assert memory.snapshots([identifier])[identifier]['content'] == '喜欢无糖咖啡'
    assert memory.index_source.eligible_ids() == {str(identifier)}
    document = memory.index_source.documents(str(identifier))[0]
    assert document.content == 'Kind: preference\nKey: drink\nContent: 喜欢无糖咖啡'
    assert document.document_type == 'confirmed_memory'
    dense.assert_awaited_once_with(
        [MemoryRecallQuery(query)], 24, eligible_ids={query: frozenset({str(identifier)})},
    )
    rerank.assert_awaited_once()
    assert not database.in_transaction


@pytest.mark.parametrize('state', ['always', 'scoped', 'expired', 'superseded', 'forgotten'])
def test_index_source_retains_private_write_candidates(database, state):
    memory = Memory(database)
    identifier = write(memory.repository)
    with transaction(database):
        if state in {'always', 'scoped'}:
            database.execute('UPDATE memories SET activation=? WHERE id=?', (state, identifier))
        elif state == 'expired':
            database.execute('UPDATE memories SET expires_at=? WHERE id=?', (time.time() - 1, identifier))
        elif state == 'superseded':
            replacement = write(memory.repository, key='replacement', text='新的约定')
            database.execute('UPDATE memories SET superseded_by=? WHERE id=?', (replacement, identifier))
        else:
            memory.repository.forget(memory.snapshots([identifier])[identifier],
                                     {'event_id': 'forget', 'quote': '忘记'}, now=time.time())
    retained = state in {"always", "scoped", "forgotten"}
    assert (str(identifier) in memory.index_source.eligible_ids()) == retained
    assert bool(memory.index_source.documents(str(identifier))) == retained
    assert memory.index_source.documents('missing') == []
    assert asyncio.run(memory.search('无糖咖啡')) == []


def test_memory_api_does_not_own_connection_or_commit_parent_transaction(database):
    memory = Memory(database)
    with pytest.raises(RuntimeError, match='abort'):
        with transaction(database):
            identifier = write(memory.repository)
            assert memory.snapshots([identifier])
            assert memory.search_literal('无糖咖啡', 6)
            assert database.in_transaction
            raise RuntimeError('abort')
    assert memory.snapshots([identifier]) == {}
    assert database.execute('SELECT 1').fetchone()[0] == 1
