"""Keep each recall unit's memory filters ahead of dense top-k and joint selection."""
import copy
import json

from ...memory import MemoryRecallQuery
from ...memory.retrieval.ranking import rank_recall_items
from ...memory.retrieval.service import MAX_MEMORY_RECALL_CANDIDATES


def query_groups(selected):
    groups = {}
    for item in selected:
        filters = item.get('memory_filters', {})
        key = json.dumps(filters, sort_keys=True)
        group = groups.setdefault(key, (filters, []))
        group[1].append(MemoryRecallQuery(
            expression=str(item['expression']),
            semantic_expression=str(item['semantic_expression']),
            unit_ids=tuple(item['unit_ids']), priority=int(item['priority']),
            kinds=tuple(item.get('kinds') or []),
        ))
    return list(groups.values())


def merge_candidates(rows):
    merged = {}
    for row in rows:
        key = (row['source'], row['id'])
        if key not in merged:
            merged[key] = copy.deepcopy(row)
            continue
        previous = merged[key]
        units = sorted(set(previous.get('unit_ids', [])) | set(row.get('unit_ids', [])))
        if row.get('search_score', 0) > previous.get('search_score', 0):
            merged[key] = copy.deepcopy(row)
        merged[key]['unit_ids'] = units
    return rank_recall_items(list(merged.values()))[:MAX_MEMORY_RECALL_CANDIDATES]


async def filtered_candidates(memory, selected, dense_evidence):
    rows = []
    async def collect(_request, candidates):
        rows.extend(candidates)
        return []
    for filters, queries in query_groups(selected):
        if all(not query.dense_expression for query in queries):
            candidates = memory.rank([], MAX_MEMORY_RECALL_CANDIDATES, filters=filters)
            for row in candidates:
                row['unit_ids'] = sorted({unit for query in queries for unit in query.unit_ids})
            rows.extend(candidates)
            continue
        await memory.search(queries, filters=filters, reranker=collect,
                            dense_evidence=None if any(filters.values()) else dense_evidence)
    return merge_candidates(rows)


def rank_candidates(memory, selected, limit, dense_evidence):
    rows = []
    for filters, queries in query_groups(selected):
        # A combined unfiltered snapshot must not bypass metadata filtering.
        rows.extend(memory.rank([query for query in queries if query.dense_expression], MAX_MEMORY_RECALL_CANDIDATES, filters=filters,
                                dense_evidence=dense_evidence))
    return merge_candidates(rows)[:limit]
