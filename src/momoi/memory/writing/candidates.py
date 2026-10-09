"""Bounded private candidates; similarity proposes comparisons, never mutations."""
from copy import deepcopy
import unicodedata

from ..retrieval.models import MemoryRecallQuery
from ..retrieval.sparse import StringSearchBackend, search_expression
from ..text import estimate_tokens
from ..storage.records import legacy_scope

MAX_REQUESTS = 8
PER_REQUEST = 8
MAX_CANDIDATES = 32
CANDIDATE_TOKENS = 8000


class CandidateBudgetExceeded(ValueError):
    pass


def normalized(text):
    return unicodedata.normalize('NFC', text).strip()


def check_budget(snapshots, forgotten):
    rows = {**snapshots, **forgotten}
    if len(rows) > MAX_CANDIDATES or sum(estimate_tokens(str(row['content'])) for row in rows.values()) > CANDIDATE_TOKENS:
        raise CandidateBudgetExceeded('memory candidate budget exceeded; split or defer this batch')


class WriteCandidates:
    def __init__(self, repository, recall):
        self.repository = repository
        self.recall = recall

    async def collect(self, context, expressions=None):
        rows = self.repository.planning_rows()
        queries = expressions or [str(request['content']) for request in context.requests]
        if len(queries) > MAX_REQUESTS:
            raise CandidateBudgetExceeded('at most eight memory requests or searches per batch')
        by_id = {row['id']: row for row in rows}
        mandatory = dict(context.snapshots)
        forgotten = dict(context.forgotten)
        pools = {}
        for query in queries:
            relevant_requests = context.requests if expressions else [r for r in context.requests if r['content'] == query]
            scopes = {r.get('scope', legacy_scope(by_id[r['target_id']]) if r.get('target_id') in by_id else None)
                      for r in relevant_requests}
            eligible = [row for row in rows if None in scopes or legacy_scope(row) in scopes]
            pools[query] = frozenset(str(row['id']) for row in eligible)
            for row in eligible:
                if (row['id'] in {r.get('target_id') for r in relevant_requests}
                        or normalized(row['content']) in {normalized(str(r['content'])) for r in relevant_requests}):
                    self._add(row, mandatory, forgotten)
        check_budget(mandatory, forgotten)
        dense = None
        if self.recall is not None and self.recall.dense_recall is not None and rows:
            dense = await self.recall.dense_recall(
                [MemoryRecallQuery(query) for query in queries], PER_REQUEST, eligible_ids=pools,
            )
        fallback = getattr(dense, 'fallback_reason', '') if dense is not None else 'disabled'
        # Preserve a failed query across subsequent successful searches in this plan.
        if fallback and context.retrieval_fallback in ('', 'disabled', 'no_active_space', 'building_initial_space'):
            context.retrieval_fallback = fallback
        additions = []
        for query in queries:
            scored = []
            for row in rows:
                if str(row['id']) not in pools[query]:
                    continue
                match = search_expression(query, (row['key'], row['content']), StringSearchBackend())
                hit = dense.memory.get(query, {}).get(('confirmed_memory', str(row['id']))) if dense else None
                thresholds = dense.thresholds('confirmed_memory') if dense else None
                cosine = hit.cosine if hit and thresholds and hit.cosine >= thresholds.support else 0.0
                if match or cosine:
                    scored.append((bool(match), cosine, row['updated_at'], row['id'], row))
            scored.sort(key=lambda item: item[:-1], reverse=True)
            additions.extend(item[-1] for item in scored[:PER_REQUEST])
        # Mandatory records cannot be silently trimmed; optional records fit the remaining budget.
        for row in additions:
            proposed, deleted = dict(mandatory), dict(forgotten)
            self._add(row, proposed, deleted)
            try:
                check_budget(proposed, deleted)
            except CandidateBudgetExceeded:
                continue
            mandatory, forgotten = proposed, deleted
        context.snapshots.clear()
        context.snapshots.update(mandatory)
        context.forgotten.clear()
        context.forgotten.update(forgotten)
        return context

    @staticmethod
    def _add(row, snapshots, forgotten):
        row = deepcopy(row)
        if row['forgotten_at'] is not None:
            forgotten[row['id']] = row
            snapshots.pop(row['id'], None)
        else:
            for key in ('forgotten_at', 'forgotten_event_id', 'forgotten_quote'):
                row.pop(key)
            snapshots[row['id']] = row
