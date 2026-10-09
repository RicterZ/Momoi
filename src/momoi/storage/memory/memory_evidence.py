class MemoryEvidenceStore:
    def memory_evidence_for_memories(
        self, memory_ids: list[int]
    ) -> list[dict[str, object]]:
        if not memory_ids:
            return []
        placeholders = ",".join("?" for _ in memory_ids)
        rows = self._db.execute(
            f"""SELECT DISTINCT e.source_event_id AS id,
                       COALESCE(v.content, e.quote) AS content,
                       COALESCE(v.occurred_at, e.created_at) AS occurred_at,
                       COALESCE(v.received_at, e.created_at) AS received_at
                FROM memory_evidence AS e
                LEFT JOIN events AS v ON v.id=e.source_event_id
                WHERE e.memory_id IN ({placeholders})
                  AND (v.id IS NOT NULL OR e.source_event_id LIKE 'dashboard:memory:%')
                ORDER BY received_at, id""",
            memory_ids,
        ).fetchall()
        return [
            {
                "event_id": str(row["id"]),
                "content": str(row["content"]),
                "occurred_at": self.context_timestamp(row["occurred_at"]),
                "received_at": float(row["received_at"]),
            }
            for row in rows
        ]

