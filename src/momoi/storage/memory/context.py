"""Momoi context rendering and reflection source adapter."""
from .presentation import REFLECTION_MEMORY_CAUTION, format_reflection_memory, format_memory
from ...memory.retrieval.models import MemoryRecallQuery


class MemoryContextStore:
    def _memory_context(self, rows):
        return "\n\n".join(format_memory(dict(row)) for row in rows)

    def always_memory_context(self) -> str:
        return self._memory_context(self.memories.repository.rows("always"))

    def scoped_memory_context(self, scope: str) -> str:
        return self._memory_context(self.memories.repository.rows("scoped", scope=scope))


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
        ranked = self.memories.rank(
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
