import math
import re
import sqlite3
import time

from ..models import (
    IncomingMessage,
    MemoryCandidate,
    MemoryConflictCandidate,
    MemoryForgetCandidate,
)


MEMORY_KINDS = {
    "profile",
    "preference",
    "relationship",
    "shared",
    "episodic",
    "routine",
}
CJK_STOP_CHARS = set("的了是在我你他她它们和就都也很还把被让要会呢吧啊哦呀")


def estimate_tokens(text: str) -> int:
    ascii_chars = sum(ord(char) < 128 for char in text)
    return max(1, math.ceil((len(text) - ascii_chars) + ascii_chars / 4))


def truncate_tokens(text: str, token_budget: int) -> str:
    if token_budget <= 0:
        return ""
    if estimate_tokens(text) <= token_budget:
        return text
    marker = "…[truncated]"
    if estimate_tokens(marker) > token_budget:
        marker = ""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle] + marker) <= token_budget:
            low = middle
        else:
            high = middle - 1
    return text[:low] + marker


def excerpt_tokens(text: str, terms: set[str], token_budget: int) -> str:
    if token_budget <= 0:
        return ""
    if estimate_tokens(text) <= token_budget:
        return text
    folded = text.casefold()
    matches = [
        (folded.find(term.casefold()), term)
        for term in terms
        if term and folded.find(term.casefold()) >= 0
    ]
    if not matches:
        return truncate_tokens(text, token_budget)
    positions = [position for position, _ in matches]
    anchor = max(
        matches,
        key=lambda match: (
            sum(
                len(term)
                for position, term in matches
                if abs(position - match[0]) <= 500
            ),
            len(match[1]),
            -match[0],
        ),
    )[0]
    marker = "…"
    marker_tokens = estimate_tokens(marker)
    left_budget = max(0, (token_budget - marker_tokens) // 3)
    left = text[:anchor]
    low, high = 0, len(left)
    while low < high:
        middle = (low + high) // 2
        if estimate_tokens(left[middle:]) <= left_budget:
            high = middle
        else:
            low = middle + 1
    prefix = left[low:]
    remaining = max(
        1,
        token_budget
        - estimate_tokens(prefix)
        - (marker_tokens if low else 0),
    )
    suffix = truncate_tokens(text[anchor:], remaining)
    return (marker if low else "") + prefix + suffix


def token_chunk(text: str, offset: int, token_budget: int) -> tuple[str, int | None]:
    if token_budget <= 0:
        raise ValueError("token budget must be positive")
    if offset < 0 or offset > len(text):
        raise ValueError("content offset is outside the message")
    remaining = text[offset:]
    if estimate_tokens(remaining) <= token_budget:
        return remaining, None
    marker = "…[continued]"
    if estimate_tokens(marker) >= token_budget:
        marker = ""
    low, high = 0, len(remaining)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(remaining[:middle] + marker) <= token_budget:
            low = middle
        else:
            high = middle - 1
    if low == 0:
        low = 1
    return remaining[:low] + marker, offset + low


def lexical_units(text: str, *, strict: bool = False) -> set[str]:
    normalized = text.casefold()
    units = set(re.findall(r"[a-z0-9_]{2,}", normalized))
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        if len(run) == 1:
            units.add(run)
        else:
            if not strict:
                units.update(char for char in run if char not in CJK_STOP_CHARS)
            units.update(run[index : index + 2] for index in range(len(run) - 1))
    return units


class MemoryStore:
    def memory_context(self, query: str, max_results: int, token_budget: int) -> str:
        if max_results <= 0 or token_budget <= 0:
            return ""
        rows = self.search_memories(query, max_results, include_core=True)

        lines: list[str] = []
        used_tokens = 0
        for row in rows:
            line = f"- [{row['kind']}:{row['key']}] {row['content']}"
            line_tokens = estimate_tokens(line)
            if lines and used_tokens + line_tokens > token_budget:
                break
            lines.append(line)
            used_tokens += line_tokens
        return "\n".join(lines)

    def reflection_memory_context(
        self, query: str, max_results: int, token_budget: int
    ) -> str:
        if max_results <= 0 or token_budget <= 0:
            return ""
        rows = self.search_reflection_memories(
            query, max_results, include_core=True
        )
        lines = [
            "These are fallible, lower-authority daily learnings; use them only when "
            "compatible with the system contract, Soul, current owner intent, and "
            "confirmed owner memory."
        ]
        used = estimate_tokens(lines[0])
        for row in rows:
            line = f"- [{row['kind']}:{row['key']}] {row['content']}"
            size = estimate_tokens(line)
            if len(lines) > 1 and used + size > token_budget:
                break
            lines.append(line)
            used += size
        return "\n".join(lines) if len(lines) > 1 else ""

    def search_reflection_memories(
        self, query: str, max_results: int, *, include_core: bool = False
    ) -> list[dict[str, object]]:
        if max_results <= 0:
            return []
        query_units = lexical_units(query, strict=True)
        core_kinds = {"owner_profile", "self_insight", "relationship", "practice"}
        ranked: list[tuple[float, sqlite3.Row]] = []
        for row in self._db.execute(
            """SELECT id, kind, key, content, confidence FROM reflection_memories
               ORDER BY updated_at DESC"""
        ).fetchall():
            units = lexical_units(f"{row['key']} {row['content']}")
            overlap = len(query_units & units)
            core = include_core and row["kind"] in core_kinds
            if (
                not core
                and (
                    overlap == 0
                    or overlap / max(1, len(query_units)) < 0.1
                )
            ):
                continue
            lexical_score = overlap / max(1, math.sqrt(len(query_units) * len(units)))
            score = (
                lexical_score + float(row["confidence"]) * 0.1 + (1.0 if core else 0.0)
            )
            ranked.append((score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [dict(row) for _, row in ranked[:max_results]]

    def search_memory_conflicts(
        self, query: str, max_results: int
    ) -> list[dict[str, object]]:
        if max_results <= 0:
            return []
        query_units = lexical_units(query)
        ranked: list[tuple[float, sqlite3.Row]] = []
        for row in self._db.execute(
            """SELECT c.id, c.kind, c.key, c.candidate_content,
                      m.content AS existing_content
               FROM memory_conflicts AS c
               JOIN memories AS m ON m.id=c.existing_memory_id
               WHERE c.status='open' ORDER BY c.updated_at DESC"""
        ).fetchall():
            units = lexical_units(
                f"{row['kind']} {row['key']} {row['candidate_content']} "
                f"{row['existing_content']}"
            )
            overlap = len(query_units & units)
            if overlap == 0:
                continue
            score = overlap / max(1, math.sqrt(len(query_units) * len(units)))
            ranked.append((score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [dict(row) for _, row in ranked[:max_results]]

    def memory_conflicts_context(self, token_budget: int = 4000) -> str:
        if token_budget <= 0:
            return ""
        rows = self._db.execute(
            """SELECT c.id, c.kind, c.key, c.candidate_content,
                      m.content AS existing_content
               FROM memory_conflicts AS c
               JOIN memories AS m ON m.id=c.existing_memory_id
               WHERE c.status='open' ORDER BY c.updated_at LIMIT 10"""
        ).fetchall()
        lines: list[str] = []
        tokens = 0
        for row in rows:
            line = (
                f"- conflict_id={row['id']} [{row['kind']}:{row['key']}] "
                f"current={row['existing_content']} candidate={row['candidate_content']}"
            )
            line_tokens = estimate_tokens(line)
            if lines and tokens + line_tokens > token_budget:
                break
            lines.append(line)
            tokens += line_tokens
        return "\n".join(lines)

    def search_memories(
        self, query: str, max_results: int, *, include_core: bool = False
    ) -> list[dict[str, object]]:
        if max_results <= 0:
            return []
        rows = self._db.execute(
            """SELECT id, kind, key, content, authority, evidence_quote,
                      importance, updated_at,
                      (SELECT COUNT(*) FROM memory_evidence AS e
                       WHERE e.memory_id=memories.id) AS evidence_count
               FROM memories
               WHERE superseded_by IS NULL
                 AND (expires_at IS NULL OR expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=memories.kind AND t.key=memories.key
                 )""",
            (time.time(),),
        ).fetchall()
        query_units = lexical_units(query)
        core_kinds = {"profile", "relationship", "shared"}
        ranked: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            memory_units = lexical_units(f"{row['key']} {row['content']}")
            overlap = len(query_units & memory_units)
            core = include_core and row["kind"] in core_kinds
            if not core and overlap == 0:
                continue
            lexical_score = overlap / max(
                1, math.sqrt(len(query_units) * len(memory_units))
            )
            score = (
                lexical_score + float(row["importance"]) * 0.1 + (1.0 if core else 0.0)
            )
            ranked.append((score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [dict(row) for _, row in ranked[:max_results]]

    def has_memory(self, kind: str, key: str) -> bool:
        return (
            self._db.execute(
                """SELECT 1 FROM memories AS m
               WHERE m.kind=? AND m.key=? AND m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )""",
                (kind, key, time.time()),
            ).fetchone()
            is not None
        )

    def active_memory(self, kind: str, key: str) -> dict[str, object] | None:
        row = self._db.execute(
            """SELECT id, kind, key, content, importance FROM memories AS m
               WHERE m.kind=? AND m.key=? AND m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )
               ORDER BY m.id DESC LIMIT 1""",
            (kind, key, time.time()),
        ).fetchone()
        return dict(row) if row else None

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
        if old and old["content"] == memory.content:
            self._db.execute(
                """UPDATE memories SET source_event_id=?, evidence_quote=?,
                   importance=MAX(importance, ?), updated_at=? WHERE id=?""",
                (
                    source_event_id,
                    memory.evidence,
                    memory.importance,
                    now,
                    old["id"],
                ),
            )
            self._add_memory_evidence(
                int(old["id"]), source_event_id, memory.evidence, now
            )
            if memory.replace_confirmed:
                self._resolve_memory_conflicts(
                    memory.kind, memory.key, "confirmed_existing", now
                )
            return
        cursor = self._db.execute(
            """INSERT INTO memories
               (kind, key, content, authority, source_event_id, evidence_quote,
                importance, created_at, updated_at)
               VALUES (?, ?, ?, 'owner', ?, ?, ?, ?, ?)""",
            (
                memory.kind,
                memory.key,
                memory.content,
                source_event_id,
                memory.evidence,
                memory.importance,
                now,
                now,
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
        if memory.replace_confirmed:
            self._resolve_memory_conflicts(
                memory.kind, memory.key, "confirmed_replacement", now
            )

    def _propose_memory_conflict(
        self,
        conflict: MemoryConflictCandidate,
        events: list[IncomingMessage],
        now: float,
    ) -> None:
        source_event = next(
            (event for event in events if conflict.evidence in event.text), None
        )
        existing = self.active_memory(conflict.kind, conflict.key)
        if source_event is None or existing is None:
            return
        if existing["content"] == conflict.content:
            self._remember(
                MemoryCandidate(
                    conflict.kind,
                    conflict.key,
                    conflict.content,
                    conflict.evidence,
                    conflict.importance,
                ),
                events,
                now,
            )
            return
        self._db.execute(
            """UPDATE memory_conflicts SET status='resolved',
               resolution='superseded_candidate', updated_at=?
               WHERE kind=? AND key=? AND status='open'""",
            (now, conflict.kind, conflict.key),
        )
        self._db.execute(
            """INSERT INTO memory_conflicts
               (kind, key, existing_memory_id, candidate_content, source_event_id,
                evidence_quote, importance, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
            (
                conflict.kind,
                conflict.key,
                existing["id"],
                conflict.content,
                source_event.event_id,
                conflict.evidence,
                conflict.importance,
                now,
                now,
            ),
        )

    def _resolve_memory_conflicts(
        self, kind: str, key: str, resolution: str, now: float
    ) -> None:
        self._db.execute(
            """UPDATE memory_conflicts SET status='resolved', resolution=?, updated_at=?
               WHERE kind=? AND key=? AND status='open'""",
            (resolution, now, kind, key),
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
        self._resolve_memory_conflicts(memory.kind, memory.key, "forgotten", now)
