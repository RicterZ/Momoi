from ..models import IncomingMessage, MemoryCandidate, MemoryForgetCandidate
from ..policies import MemoryPolicy
from .memory_values import MEMORY_ACTIVATIONS, MEMORY_KINDS, memory_expires_at


class MemoryMutationStore:
    _memory_policy: MemoryPolicy

    def _remember(
        self,
        memory: MemoryCandidate,
        events: list[IncomingMessage],
        now: float,
    ) -> None:
        source_event = next(
            (event for event in events if memory.evidence in event.text), None
        )
        if (
            memory.kind not in MEMORY_KINDS
            or memory.activation not in MEMORY_ACTIVATIONS
            or not all((memory.key, memory.content, memory.evidence))
            or source_event is None
            or len(memory.key) > 200
            or len(memory.content) > 2000
            or len(memory.evidence) > 500
        ):
            return
        source_event_id = source_event.event_id
        self._db.execute(
            "DELETE FROM memory_tombstones WHERE kind=? AND key=?",
            (memory.kind, memory.key),
        )
        old = self._db.execute(
            """SELECT id, content FROM memories
               WHERE kind=? AND key=? AND superseded_by IS NULL
               ORDER BY id DESC LIMIT 1""",
            (memory.kind, memory.key),
        ).fetchone()
        expires_at = memory_expires_at(
            memory.activation, memory.ttl_hours, now, self._memory_policy
        )
        if old and old["content"] == memory.content:
            self._db.execute(
                """UPDATE memories SET source_event_id=?, evidence_quote=?,
                   activation=?, expires_at=?, importance=MAX(importance, ?),
                   updated_at=?
                   WHERE id=?""",
                (
                    source_event_id,
                    memory.evidence,
                    memory.activation,
                    expires_at,
                    memory.importance,
                    now,
                    old["id"],
                ),
            )
            self._add_memory_evidence(
                int(old["id"]), source_event_id, memory.evidence, now
            )
            return
        cursor = self._db.execute(
            """INSERT INTO memories
               (kind, key, content, activation, authority, source_event_id,
                evidence_quote, importance, created_at, updated_at, expires_at)
               VALUES (?, ?, ?, ?, 'owner', ?, ?, ?, ?, ?, ?)""",
            (
                memory.kind,
                memory.key,
                memory.content,
                memory.activation,
                source_event_id,
                memory.evidence,
                memory.importance,
                now,
                now,
                expires_at,
            ),
        )
        if old:
            self._db.execute(
                "UPDATE memories SET superseded_by=?, updated_at=? WHERE id=?",
                (cursor.lastrowid, now, old["id"]),
            )
        self._add_memory_evidence(
            int(cursor.lastrowid), source_event_id, memory.evidence, now
        )

    def _add_memory_evidence(
        self,
        memory_id: int,
        source_event_id: str,
        quote: str,
        now: float,
    ) -> None:
        self._db.execute(
            """INSERT OR IGNORE INTO memory_evidence
               (memory_id, source_event_id, quote, created_at)
               VALUES (?, ?, ?, ?)""",
            (memory_id, source_event_id, quote, now),
        )

    def _forget_memory(
        self,
        memory: MemoryForgetCandidate,
        events: list[IncomingMessage],
        now: float,
    ) -> None:
        source_event = next(
            (event for event in events if memory.evidence in event.text), None
        )
        if source_event is None:
            return
        self._db.execute(
            """INSERT INTO memory_tombstones
               (kind, key, source_event_id, evidence_quote, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(kind, key) DO UPDATE SET
                 source_event_id=excluded.source_event_id,
                 evidence_quote=excluded.evidence_quote,
                 created_at=excluded.created_at""",
            (memory.kind, memory.key, source_event.event_id, memory.evidence, now),
        )

