"""Temporary Store entry points and Momoi-specific recall rendering."""
from .memory_values import (
    REFLECTION_MEMORY_CAUTION, MemoryRecallQuery, format_reflection_memory, format_memory,
)
from ...memory.retrieval.service import MAX_MEMORY_RECALL_RESULTS


class MemoryRecallStore:
    def rank_recalled_memories(self, queries, max_results, *, now=None,
                               include_reflections=False, dense_evidence=None):
        return self.memory_recall.rank(
            queries, max_results, now=now, include_reflections=include_reflections,
            dense_evidence=dense_evidence,
        )

    def search_memories(self, query, max_results, *, include_core=False,
                        activation=None, include_scoped=False):
        return self.memory_recall.search_literal(
            query, max_results, include_core=include_core,
            activation=activation, include_scoped=include_scoped,
        )

    def _reflection_recall_rows(self):
        return self._db.execute(
            """SELECT rm.id, rm.kind, rm.key, rm.content, rm.confidence, rm.evidence,
                      rm.updated_at, r.local_date
               FROM reflection_memories AS rm
               LEFT JOIN reflections AS r ON r.id=rm.source_reflection_id
               WHERE NOT EXISTS (
                   SELECT 1 FROM reflection_memory_tombstones AS t
                   WHERE t.kind=rm.kind AND t.key=rm.key
               )
               ORDER BY rm.updated_at DESC"""
        ).fetchall()

    def ranked_memory_context(
        self,
        query: str,
        max_results: int,
        *,
        include_reflections: bool = False,
    ) -> tuple[str, str]:
        """Render independently ranked confirmed and reflection result sets."""

        if max_results <= 0 or not query.strip():
            return "", ""
        ranked = self.rank_recalled_memories(
            [MemoryRecallQuery(query.strip())],
            max_results,
            include_reflections=include_reflections,
        )
        confirmed: list[str] = []
        reflected: list[str] = []
        reflection_header = REFLECTION_MEMORY_CAUTION
        for row in ranked:
            line = (
                format_reflection_memory(row)
                if row["source"] == "reflection"
                else format_memory(row)
            )
            if row["source"] == "confirmed":
                confirmed.append(line)
            else:
                reflected.append(line)
        return (
            "\n".join(confirmed),
            "\n".join([reflection_header, *reflected]) if reflected else "",
        )
