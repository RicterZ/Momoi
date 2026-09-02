import json
import time

from .integrity import decode_stored_json
from .timestamps import add_context_timestamps


class ReflectionRecordStore:
    def commit_reflection(
        self,
        local_date: str,
        turn_id: str,
        summary: str,
        memories: list[dict[str, object]],
        conversation_actions: list[dict[str, object]] | None = None,
        maintenance_turn_id: str = "",
    ) -> None:
        reflection_id = f"reflection:{local_date}"
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE reflections SET state='completed', claimed_at=NULL,
                   retry_at=NULL, summary=?, memories_json=?, error=NULL,
                   completed_at=? WHERE id=? AND state='running'""",
                (
                    summary,
                    json.dumps(memories, ensure_ascii=False, separators=(",", ":")),
                    now,
                    reflection_id,
                ),
            )
            self._db.execute(
                "DELETE FROM reflection_memories WHERE source_reflection_id=?",
                (reflection_id,),
            )
            for memory in memories:
                self._db.execute(
                    """INSERT INTO reflection_memories
                       (kind, key, content, evidence, confidence,
                        source_reflection_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(kind, key) DO UPDATE SET
                         content=excluded.content,
                         evidence=excluded.evidence,
                         confidence=excluded.confidence,
                         source_reflection_id=excluded.source_reflection_id,
                         updated_at=excluded.updated_at""",
                    (
                        memory["kind"],
                        memory["key"],
                        memory["content"],
                        memory["evidence"],
                        memory["confidence"],
                        reflection_id,
                        now,
                        now,
                    ),
                )
            self.apply_conversation_actions(conversation_actions or [], now=now)
            if maintenance_turn_id:
                self._db.execute(
                    """INSERT OR IGNORE INTO turns
                       (id, kind, workflow_kind, source_ids_json, state, stage,
                        started_at, updated_at)
                       VALUES (?, 'autonomous', 'memory_maintenance', ?, 'running',
                               'memory_maintenance_queued', ?, ?)""",
                    (
                        maintenance_turn_id,
                        json.dumps([reflection_id]),
                        now,
                        now,
                    ),
                )
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   failure_reason=NULL, updated_at=? WHERE id=?""",
                (now, turn_id),
            )

    def reflection(self, local_date: str) -> dict[str, object] | None:
        row = self._db.execute(
            "SELECT * FROM reflections WHERE local_date=?", (local_date,)
        ).fetchone()
        return dict(row) if row else None

    def list_reflections(
        self, limit: int = 14, *, before: str | None = None
    ) -> dict[str, object]:
        if limit <= 0:
            return {"items": []}
        size = min(366, max(1, int(limit)))
        query = "SELECT * FROM reflections"
        params: list[object] = []
        cursor = str(before or "").strip()
        if cursor:
            query += " WHERE local_date < ?"
            params.append(cursor)
        query += " ORDER BY local_date DESC LIMIT ?"
        params.append(size + 1)
        rows = self._db.execute(query, params).fetchall()
        extra = len(rows) > size
        results: list[dict[str, object]] = []
        for row in rows[:size]:
            item = dict(row)
            item["memories"] = decode_stored_json(
                item.pop("memories_json", "[]"),
                entity="reflection",
                record_id=item["id"],
                field="memories_json",
                expected_type=list,
                fallback=[],
            )
            add_context_timestamps(
                item,
                ("scheduled_at", "retry_at", "created_at", "completed_at"),
                self._timezone,
            )
            results.append(item)
        payload: dict[str, object] = {"items": results}
        if extra and results:
            payload["next_cursor"] = results[-1]["local_date"]
        return payload

