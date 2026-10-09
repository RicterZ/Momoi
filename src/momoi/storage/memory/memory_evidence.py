class MemoryEvidenceStore:
    def memory_evidence_for_memories(
        self, memory_ids: list[int]
    ) -> list[dict[str, object]]:
        if not memory_ids:
            return []
        placeholders = ",".join("?" for _ in memory_ids)
        rows = self._db.execute(
            f"""SELECT DISTINCT v.id, v.content, v.occurred_at, v.received_at
                FROM memory_evidence AS e
                JOIN events AS v ON v.id=e.source_event_id
                WHERE e.memory_id IN ({placeholders})
                ORDER BY v.received_at, v.id""",
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

