"""Controlled tags, current metadata filters, and bounded rerank candidates."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from momoi.memory import Memory, MemoryRecallQuery, TagCatalog
from momoi.memory.retrieval.dense import DenseQueryService, MemoryVectorRecall
from momoi.memory.retrieval.models import DenseThresholds
from momoi.memory.retrieval.snapshot import SegmentedVectorSnapshot
from momoi.memory.storage.records import memory_snapshot_fingerprint
from momoi.memory.storage.transactions import transaction
from tests.test_memory_dense import vectors, add_vector
from tests.test_memory_repository import database, write


CATALOG = TagCatalog({'food_drink': '饮食', 'health': '健康', 'work': '工作', 'social': '社交'})


def tag(memory, identifier, tags):
    snapshot = memory.snapshots([identifier])[identifier]
    memory.repository.update_meta(snapshot, {'tags': tags})


@pytest.mark.parametrize('meta', [None, [], {'scope': 'global'}, {'tags': 'food_drink'},
                                 {'tags': ['invented']}, {'tags': ['health', 'health']},
                                 {'tags': [[]]}, {'tags': list(CATALOG.tags)}])
def test_reject_invalid_metadata_before_writing(database, meta):
    memory = Memory(database, tags=CATALOG)
    identifier = write(memory.repository)
    original = memory.snapshots([identifier])[identifier]
    with pytest.raises(ValueError):
        memory.repository.write({**original, 'meta': meta}, [identifier],
                                {'event_id': 'event', 'quote': '饮食'}, [], now=20)
    assert memory.snapshots([identifier])[identifier] == original
    assert database.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 1


@pytest.mark.parametrize('filters', [{'tags_any': ['unknown']}, {'tags_any': 'health'},
                                    {'kinds': ['unknown']}, {'scope': 'heartbeat'},
                                    {'tags_any': [None]}, {'tags_any': ['health', 'health']}])
def test_invalid_filters_do_not_silently_expand_search(database, filters):
    memory = Memory(database, tags=CATALOG, dense_recall=AsyncMock())
    with pytest.raises(ValueError):
        asyncio.run(memory.search('饮品', filters=filters))
    memory.recall.dense_recall.assert_not_awaited()
    with pytest.raises(ValueError):
        memory.search_literal('饮品', 6, filters=filters)


def test_sparse_filters_intersect_fields_and_keep_untagged_defaults(database):
    memory = Memory(database, tags=CATALOG)
    coffee = write(memory.repository)
    water = write(memory.repository, key='water', text='无糖咖啡旁边放水')
    untagged = write(memory.repository, key='untagged')
    tag(memory, coffee, ['food_drink'])
    tag(memory, water, ['health'])
    with transaction(database):
        database.execute("UPDATE memories SET kind='practice' WHERE id=?", (water,))
    assert {r['id'] for r in asyncio.run(memory.search('无糖咖啡'))} == {coffee, water, untagged}
    filters = {'tags_any': ['food_drink', 'health'], 'kinds': ['preference']}
    assert [r['id'] for r in asyncio.run(memory.search('无糖咖啡', filters=filters))] == [coffee]
    assert [r['id'] for r in memory.search_literal('无糖咖啡', 6, filters=filters)] == [coffee]
    assert asyncio.run(memory.search('无糖咖啡', filters={'tags_any': ['social']})) == []
    assert len(asyncio.run(memory.search('无糖咖啡', filters={'tags_any': [], 'kinds': []}))) == 3
    assert memory.repository.inventory()[0]['meta'] == {'tags': ['food_drink']}
    assert memory.repository.rows('recall')[0]['meta'] == {'tags': ['food_drink']}
    # Reading historical metadata is independent of a later catalog change.
    assert Memory(database).snapshots([coffee])[coffee]['meta'] == {'tags': ['food_drink']}


@pytest.mark.parametrize('constraint', ['tags', 'kinds', 'query_kinds', 'visibility'])
def test_filters_precede_dense_top_k_and_read_current_metadata(database, vectors, constraint):
    memory = Memory(database, tags=CATALOG)
    desired = write(memory.repository)
    add_vector(database, desired, (.8, .6))
    tag(memory, desired, ['food_drink'])
    for index in range(10):
        unwanted = write(memory.repository, key=f'noise{index}', text='无关内容')
        add_vector(database, unwanted)
        if constraint in {'kinds', 'query_kinds'}:
            with transaction(database):
                database.execute("UPDATE memories SET kind='practice' WHERE id=?", (unwanted,))
        elif constraint == 'visibility':
            with transaction(database):
                database.execute("UPDATE memories SET activation='always' WHERE id=?", (unwanted,))
    snapshot = SegmentedVectorSnapshot(vectors, 2)
    snapshot.load('space')
    encoder = AsyncMock()
    encoder.encode.return_value = [[1.0, 0.0]]
    memory.recall.dense_recall = MemoryVectorRecall(
        DenseQueryService(snapshot, encoder, candidate_floor=1, candidate_multiplier=1),
        {'confirmed_memory': DenseThresholds(.5, .7, .9)},
    )
    filters = {'tags_any': ['food_drink']} if constraint == 'tags' else (
        {'kinds': ['preference']} if constraint == 'kinds' else None)
    query = [MemoryRecallQuery('用户喜欢什么饮品', kinds=('preference',))] if constraint == 'query_kinds' else '用户喜欢什么饮品'
    results = asyncio.run(memory.search(query, limit=1, filters=filters))
    assert [r['id'] for r in results] == [desired]
    assert results[0]['channels'] == ['dense']
    if constraint == 'tags':
        tag(memory, desired, ['health'])
        assert asyncio.run(memory.search(query, limit=1, filters=filters)) == []
        assert [r['id'] for r in asyncio.run(memory.search(query, limit=1, filters={'tags_any': ['health']}))] == [desired]
        # The vector snapshot was never refreshed after retagging.
        assert encoder.encode.await_count == 3


def test_meta_edits_invalidate_snapshots_and_compose_transactions(database):
    memory = Memory(database, tags=CATALOG)
    identifier = write(memory.repository)
    original = memory.snapshots([identifier])[identifier]
    document = memory.index_source.documents(str(identifier))
    with pytest.raises(RuntimeError, match='abort'):
        with transaction(database):
            memory.repository.update_meta(original, {'tags': ['health']})
            assert memory.snapshots([identifier])[identifier]['meta'] == {'tags': ['health']}
            raise RuntimeError('abort')
    assert memory.snapshots([identifier])[identifier] == original
    # Even when timestamps coincide, meta is part of the fingerprint.
    memory.repository.update_meta(original, {'tags': ['food_drink']}, now=original['updated_at'])
    current = memory.snapshots([identifier])[identifier]
    assert memory_snapshot_fingerprint(current) != memory_snapshot_fingerprint(original)
    with pytest.raises(ValueError, match='memory_snapshot_changed'):
        memory.repository.update_meta(original, {'tags': ['health']})
    assert memory.index_source.documents(str(identifier)) == document
    replacement = write(memory.repository, targets=[identifier])
    assert memory.snapshots([replacement])[replacement]['meta'] == {'tags': ['food_drink']}
    snapshot = memory.snapshots([replacement])[replacement]
    memory.repository.write({**snapshot, 'meta': {'tags': []}}, [replacement],
                            {'event_id': 'new', 'quote': '无糖'}, [], now=30)
    assert memory.repository.active('preference', 'drink')['meta'] == {'tags': []}


def test_reranker_sees_bounded_wider_pool_and_final_limit_is_enforced(database):
    from tests.test_memory_recall_service import dense

    memory = Memory(database)
    ids = [write(memory.repository, key=f'drink{index}') for index in range(30)]
    evidence = dense('饮品', *ids)
    candidates = memory.rank([MemoryRecallQuery('饮品')], 24, dense_evidence=evidence)
    reranker = AsyncMock(side_effect=lambda request, rows: list(reversed(rows)))
    results = asyncio.run(memory.search('饮品', limit=2, reranker=reranker, dense_evidence=evidence))
    assert len(reranker.await_args.args[1]) == 24
    assert results == list(reversed(candidates))[:2]
    assert len(asyncio.run(memory.search('饮品', dense_evidence=evidence))) == 6


def test_filtered_search_rejects_unfiltered_prepared_evidence(database):
    memory = Memory(database, tags=CATALOG)
    with pytest.raises(ValueError, match='before top-k'):
        asyncio.run(memory.search('饮品', filters={'tags_any': ['health']}, dense_evidence=object()))


def test_maintenance_merge_preserves_tags_and_rejects_overflow_atomically(database):
    memory = Memory(database, tags=CATALOG)
    survivor = write(memory.repository)
    other = write(memory.repository, key='other')
    tag(memory, survivor, ['food_drink', 'health'])
    tag(memory, other, ['social', 'work'])
    snapshots = memory.snapshots([survivor, other])
    events = [{'id': 'new', 'content': '咖啡', 'occurred_at': 20}]
    with pytest.raises(ValueError, match='at most three'):
        memory.repository.merge(survivor, [other], '咖啡', 'recall', None, events)
    assert memory.repository.validate_snapshots(snapshots) == snapshots
    tag(memory, other, ['social'])
    memory.repository.merge(survivor, [other], '咖啡', 'recall', None, events)
    assert memory.snapshots([survivor])[survivor]['meta'] == {'tags': ['food_drink', 'health', 'social']}
    assert memory.snapshots([other]) == {}
