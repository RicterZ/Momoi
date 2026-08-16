import sqlite3
import time

from ..context_time import context_timestamp
from ..policies import MemoryPolicy
from ..search import search_expression
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
MEMORY_ACTIVATIONS = {"always", "recent", "recall"}
ALWAYS_MEMORY_KINDS = {"profile", "preference", "relationship"}
RECENT_MEMORY_WINDOW_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_MEMORY_POLICY = MemoryPolicy()
RECENT_MEMORY_MIN_TTL_HOURS = _DEFAULT_MEMORY_POLICY.recent_min_ttl_hours
RECENT_MEMORY_MAX_TTL_HOURS = _DEFAULT_MEMORY_POLICY.recent_max_ttl_hours
ALWAYS_MEMORY_TOKEN_BUDGET = 1200


def memory_expires_at(
    activation: str,
    ttl_hours: float,
    now: float,
    policy: MemoryPolicy = _DEFAULT_MEMORY_POLICY,
) -> float | None:
    if activation != "recent":
        return None
    hours = min(
        policy.recent_max_ttl_hours,
        max(policy.recent_min_ttl_hours, float(ttl_hours)),
    )
    return now + hours * 3600


def _merged_always_memory_content(target: str, source: str) -> str:
    if source in target:
        return target
    if target in source:
        return source[:2000]
    return f"{target}；{source}"[:2000]


def estimate_tokens(text: str) -> int:
    from ..runtime.budget import TEXT_SIZER

    return TEXT_SIZER.estimate(text)


def truncate_tokens(text: str, token_budget: int) -> str:
    from ..runtime.budget import MEMORY_TEXT_FITTER

    return MEMORY_TEXT_FITTER.truncate(text, token_budget)


def excerpt_tokens(text: str, terms: set[str], token_budget: int) -> str:
    from ..runtime.budget import MEMORY_TEXT_FITTER

    return MEMORY_TEXT_FITTER.excerpt(text, terms, token_budget)


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


class MemoryStore:
    _memory_policy: MemoryPolicy

    def purge_expired_memories(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        cutoff = now - RECENT_MEMORY_WINDOW_SECONDS
        rows = self._db.execute(
            """SELECT id FROM memories AS m
               WHERE m.superseded_by IS NULL
                 AND (m.expires_at IS NOT NULL AND m.expires_at <= ?
                      OR m.activation='recent' AND m.expires_at IS NULL
                         AND m.updated_at < ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )""",
            (now, cutoff),
        ).fetchall()
        if not rows:
            return 0
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        self._db.execute(
            f"DELETE FROM memory_evidence WHERE memory_id IN ({placeholders})",
            ids,
        )
        self._db.execute(
            f"DELETE FROM memory_conflicts WHERE existing_memory_id IN ({placeholders})",
            ids,
        )
        self._db.execute(
            f"DELETE FROM memories WHERE id IN ({placeholders})", ids
        )
        self._db.commit()
        return len(ids)

    def _memory_rows(
        self, activation: str, *, now: float | None = None
    ) -> list[sqlite3.Row]:
        if activation not in MEMORY_ACTIVATIONS:
            raise ValueError("invalid memory activation")
        now = time.time() if now is None else now
        recent_cutoff = now - RECENT_MEMORY_WINDOW_SECONDS
        return self._db.execute(
            """SELECT id, kind, key, content, activation, importance, updated_at
               FROM memories AS m
               WHERE m.activation=? AND m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND (m.activation<>'recent' OR m.updated_at>=?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )
               ORDER BY m.importance DESC, m.updated_at DESC, m.id DESC""",
            (activation, now, recent_cutoff),
        ).fetchall()

    @staticmethod
    def _compact_memory_context(
        label: str, rows: list[sqlite3.Row], token_budget: int
    ) -> str:
        if token_budget <= 0 or not rows:
            return ""
        contents: list[str] = []
        seen: set[str] = set()
        for row in rows:
            content = " ".join(str(row["content"]).split())
            if not content or content in seen:
                continue
            seen.add(content)
            contents.append(content)
        if not contents:
            return ""
        return truncate_tokens(
            f"{label}：" + "；".join(contents), token_budget
        )

    def always_memory_context(self, token_budget: int = ALWAYS_MEMORY_TOKEN_BUDGET) -> str:
        return self._compact_memory_context(
            "老师的固定偏好与约束", self._memory_rows("always"), token_budget
        )

    def always_memory_inventory(self) -> list[dict[str, object]]:
        now = time.time()
        rows = self._db.execute(
            """SELECT id, kind, key, content, activation, evidence_quote,
                      importance, updated_at
               FROM memories AS m
               WHERE m.activation='always' AND m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )
               ORDER BY m.importance DESC, m.updated_at DESC, m.id DESC""",
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]

    def always_memory_count(self) -> int:
        return len(self.always_memory_inventory())

    def always_memory_inventory_context(self) -> str:
        rows = self.always_memory_inventory()
        if not rows:
            return "No always-on owner memories are stored."
        lines = [
            "Full inventory of confirmed always-on owner memories. These currently "
            "inject into every Turn. Use memory_id in always_memory_actions. "
            "Near-duplicate preferences should be merged into one concise content."
        ]
        for row in rows:
            updated = context_timestamp(row["updated_at"])
            evidence = " ".join(str(row["evidence_quote"] or "").split())
            lines.append(
                f"memory_id={row['id']} [{row['kind']}:{row['key']}] {row['content']} "
                f"evidence={evidence} updated={updated}"
            )
        return "\n".join(lines)

    def apply_always_memory_actions(
        self,
        actions: list[dict[str, object]],
        *,
        source_id: str,
        now: float,
    ) -> None:
        if not actions:
            return
        inventory = {
            int(item["id"]): item for item in self.always_memory_inventory()
        }
        merges = [item for item in actions if item["action"] == "merge"]
        demotes = [
            item
            for item in actions
            if item["action"] in {"demote_recent", "demote_recall"}
        ]
        forgets = [item for item in actions if item["action"] == "forget"]
        for item in merges:
            source = inventory.get(int(item["memory_id"]))
            target = inventory.get(int(item["merge_into_id"]))
            if source is None or target is None:
                continue
            content = str(item.get("content") or "").strip()[:2000]
            if not content:
                content = _merged_always_memory_content(
                    str(target["content"]), str(source["content"])
                )
            if content != target["content"]:
                self._db.execute(
                    "UPDATE memories SET content=?, updated_at=? WHERE id=?",
                    (content, now, target["id"]),
                )
                target["content"] = content
            self._db.execute(
                "UPDATE memories SET superseded_by=?, updated_at=? WHERE id=?",
                (target["id"], now, source["id"]),
            )
            inventory.pop(int(source["id"]), None)
        for item in demotes:
            memory = inventory.get(int(item["memory_id"]))
            if memory is None:
                continue
            activation = (
                "recent" if item["action"] == "demote_recent" else "recall"
            )
            expires_at = (
                memory_expires_at(
                    activation,
                    self._memory_policy.recent_max_ttl_hours,
                    now,
                    self._memory_policy,
                )
                if activation == "recent"
                else None
            )
            self._db.execute(
                """UPDATE memories SET activation=?, expires_at=?
                   WHERE id=? AND activation='always' AND superseded_by IS NULL""",
                (activation, expires_at, memory["id"]),
            )
        for item in forgets:
            memory = inventory.get(int(item["memory_id"]))
            if memory is None:
                continue
            self._db.execute(
                """INSERT INTO memory_tombstones
                   (kind, key, source_event_id, evidence_quote, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(kind, key) DO UPDATE SET
                     source_event_id=excluded.source_event_id,
                     evidence_quote=excluded.evidence_quote,
                     created_at=excluded.created_at""",
                (
                    memory["kind"],
                    memory["key"],
                    source_id,
                    str(item["reason"]),
                    now,
                ),
            )
            self._resolve_memory_conflicts(
                str(memory["kind"]), str(memory["key"]), "forgotten", now
            )

    def apply_recent_memory_actions(
        self,
        actions: list[dict[str, object]],
        *,
        source_id: str,
        now: float,
    ) -> None:
        for item in actions:
            row = self._db.execute(
                """SELECT id, kind, key, content FROM memories
                   WHERE id=? AND activation='recent' AND superseded_by IS NULL""",
                (int(item["memory_id"]),),
            ).fetchone()
            if row is None:
                continue
            action = item["action"]
            if action == "extend":
                expires_at = memory_expires_at(
                    "recent", float(item["ttl_hours"]), now, self._memory_policy
                )
                self._db.execute(
                    "UPDATE memories SET expires_at=?, updated_at=? WHERE id=?",
                    (expires_at, now, row["id"]),
                )
            elif action == "promote_recall":
                self._db.execute(
                    "UPDATE memories SET activation='recall', expires_at=NULL, updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
            elif action == "forget":
                self._db.execute(
                    """INSERT INTO memory_tombstones
                       (kind, key, source_event_id, evidence_quote, created_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(kind, key) DO UPDATE SET
                         source_event_id=excluded.source_event_id,
                         evidence_quote=excluded.evidence_quote,
                         created_at=excluded.created_at""",
                    (row["kind"], row["key"], source_id, str(item["reason"]), now),
                )
                self._resolve_memory_conflicts(row["kind"], row["key"], "forgotten", now)

    def recent_memory_context(self, token_budget: int) -> str:
        self.purge_expired_memories()
        return self._compact_memory_context(
            "老师近期需要保持的上下文", self._memory_rows("recent"), token_budget
        )

    def recent_memory_inventory_context(self) -> str:
        self.purge_expired_memories()
        rows = self._db.execute(
            """SELECT id, kind, key, content, expires_at, updated_at
               FROM memories AS m
               WHERE m.activation='recent' AND m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND (m.expires_at IS NOT NULL OR m.updated_at >= ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )
               ORDER BY m.updated_at DESC, m.id DESC LIMIT 32""",
            (time.time(), time.time() - RECENT_MEMORY_WINDOW_SECONDS),
        ).fetchall()
        if not rows:
            return "No active recent memories are stored."
        return "\n".join(
            f"memory_id={row['id']} [{row['kind']}:{row['key']}] "
            f"{row['content']} expires_at={context_timestamp(row['expires_at']) if row['expires_at'] else 'legacy-window'}"
            for row in rows
        )

    def memory_context(self, query: str, max_results: int, token_budget: int) -> str:
        if max_results <= 0 or token_budget <= 0:
            return ""
        rows = self.search_memories(query, max_results, activation="recall")

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

    def core_reflection_memory_context(self, token_budget: int = 900) -> str:
        if token_budget <= 0:
            return ""
        rows = self._db.execute(
            """SELECT kind, key, content, confidence
               FROM reflection_memories
               WHERE kind IN ('owner_profile', 'self_insight', 'relationship', 'practice')
               ORDER BY confidence DESC, updated_at DESC, id DESC"""
        ).fetchall()
        lines = [
            "These are stable, fallible Momoi learnings; owner-confirmed memory and "
            "the current owner input always take precedence."
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
        core_kinds = {"owner_profile", "self_insight", "relationship", "practice"}
        ranked: list[tuple[float, sqlite3.Row]] = []
        for row in self._db.execute(
            """SELECT id, kind, key, content, confidence FROM reflection_memories
               ORDER BY updated_at DESC"""
        ).fetchall():
            match = search_expression(
                query,
                (str(row["key"]), str(row["content"])),
                self._search_backend,
            )
            core = include_core and row["kind"] in core_kinds
            if not core and match is None:
                continue
            score = (
                (match.score if match else 0.0)
                + float(row["confidence"]) * 0.1
                + (1.0 if core else 0.0)
            )
            ranked.append((score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [dict(row) for _, row in ranked[:max_results]]

    def search_memory_conflicts(
        self, query: str, max_results: int
    ) -> list[dict[str, object]]:
        if max_results <= 0:
            return []
        ranked: list[tuple[float, sqlite3.Row]] = []
        for row in self._db.execute(
            """SELECT c.id, c.kind, c.key, c.activation, c.candidate_content,
                      m.content AS existing_content
               FROM memory_conflicts AS c
               JOIN memories AS m ON m.id=c.existing_memory_id
               WHERE c.status='open' ORDER BY c.updated_at DESC"""
        ).fetchall():
            match = search_expression(
                query,
                (
                    str(row["kind"]),
                    str(row["key"]),
                    str(row["candidate_content"]),
                    str(row["existing_content"]),
                ),
                self._search_backend,
            )
            if match is None:
                continue
            ranked.append((match.score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [dict(row) for _, row in ranked[:max_results]]

    def memory_conflicts_context(self, token_budget: int = 4000) -> str:
        if token_budget <= 0:
            return ""
        rows = self._db.execute(
            """SELECT c.id, c.kind, c.key, c.activation, c.candidate_content,
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
        self,
        query: str,
        max_results: int,
        *,
        include_core: bool = False,
        activation: str | None = None,
    ) -> list[dict[str, object]]:
        if max_results <= 0:
            return []
        if activation is not None and activation not in MEMORY_ACTIVATIONS:
            raise ValueError("invalid memory activation")
        self.purge_expired_memories()
        rows = self._db.execute(
            """SELECT id, kind, key, content, authority, evidence_quote,
                      activation, importance, updated_at,
                      (SELECT COUNT(*) FROM memory_evidence AS e
                       WHERE e.memory_id=memories.id) AS evidence_count
               FROM memories
               WHERE superseded_by IS NULL
                 AND (expires_at IS NULL OR expires_at > ?)
                 AND (activation<>'recent' OR updated_at>=?)
                 AND (? IS NULL OR activation=?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=memories.kind AND t.key=memories.key
                 )""",
            (time.time(), time.time() - RECENT_MEMORY_WINDOW_SECONDS, activation, activation),
        ).fetchall()
        core_kinds = {"profile", "relationship", "shared"}
        ranked: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            match = search_expression(
                query,
                (str(row["key"]), str(row["content"])),
                self._search_backend,
            )
            core = include_core and row["kind"] in core_kinds
            if not core and match is None:
                continue
            score = (
                (match.score if match else 0.0)
                + float(row["importance"]) * 0.1
                + (1.0 if core else 0.0)
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
            if memory.replace_confirmed:
                self._resolve_memory_conflicts(
                    memory.kind, memory.key, "confirmed_existing", now
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
                    False,
                    conflict.activation,
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
               (kind, key, activation, existing_memory_id, candidate_content,
                source_event_id, evidence_quote, importance, status, created_at,
                updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
            (
                conflict.kind,
                conflict.key,
                conflict.activation,
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
