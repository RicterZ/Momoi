import json
import sqlite3
import time

from ..core.integrity import decode_stored_json
from ..core.timestamps import add_context_timestamps


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
        self, limit: int = 14, *, before: str | None = None, local_date: str | None = None
    ) -> dict[str, object]:
        if limit <= 0:
            return {"items": []}
        size = min(366, max(1, int(limit)))
        query = "SELECT * FROM reflections"
        params: list[object] = []
        cursor = str(before or "").strip()
        if local_date:
            query += " WHERE local_date = ?"
            params.append(local_date)
        elif cursor:
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

    def list_reflection_memories(self) -> list[dict[str, object]]:
        rows = self._db.execute(
            """SELECT m.*, r.local_date FROM reflection_memories AS m
               JOIN reflections AS r ON r.id=m.source_reflection_id
               WHERE NOT EXISTS (
                   SELECT 1 FROM reflection_memory_tombstones AS t
                   WHERE t.kind=m.kind AND t.key=m.key
               )
               ORDER BY m.updated_at DESC, m.id DESC"""
        ).fetchall()
        return [self._reflection_memory_public_dict(row) for row in rows]

    def _reflection_memory_public_dict(self, row: sqlite3.Row) -> dict[str, object]:
        item = dict(row)
        add_context_timestamps(item, ("created_at", "updated_at"), self._timezone)
        return item

    def update_reflection_memory_content(
        self, memory_id: int, content: str
    ) -> dict[str, object] | None:
        text = content.strip()
        if not text or len(text) > 1000:
            raise ValueError("content must contain between 1 and 1000 characters")
        with self._db:
            self._db.execute(
                "UPDATE reflection_memories SET content=?, updated_at=? WHERE id=?",
                (text, time.time(), memory_id),
            )
        row = self._db.execute(
            """SELECT m.*, r.local_date FROM reflection_memories AS m
               JOIN reflections AS r ON r.id=m.source_reflection_id
               WHERE m.id=? AND NOT EXISTS (
                   SELECT 1 FROM reflection_memory_tombstones AS t
                   WHERE t.kind=m.kind AND t.key=m.key
               )""",
            (memory_id,),
        ).fetchone()
        return self._reflection_memory_public_dict(row) if row else None

    def delete_reflection_memory(self, memory_id: int) -> bool:
        """Forget one insight permanently.

        The row is removed and a tombstone records why, so regenerating that
        day's reflection cannot derive the forgotten insight back into view.
        The regeneration path that replaces a day's whole set is not a user
        deletion and deliberately leaves no tombstone.
        """
        with self._db:
            row = self._db.execute(
                "SELECT kind, key, evidence FROM reflection_memories WHERE id=?",
                (memory_id,),
            ).fetchone()
            if row is None:
                return False
            self._db.execute(
                """INSERT INTO reflection_memory_tombstones
                   (kind, key, evidence_quote, created_at) VALUES (?,?,?,?)
                   ON CONFLICT(kind, key) DO UPDATE SET
                     evidence_quote=excluded.evidence_quote,
                     created_at=excluded.created_at""",
                (row["kind"], row["key"], str(row["evidence"])[:500], time.time()),
            )
            self._db.execute("DELETE FROM reflection_memories WHERE id=?", (memory_id,))
        return True
