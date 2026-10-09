import sqlite3
import time
from dataclasses import asdict

from ..core.timestamps import add_context_timestamps

def _dashboard_unix(value: object) -> float | None:
    if value is None:
        return None
    stamp = float(value)
    return stamp if stamp > 0 else None


class DashboardStore:
    def dashboard_current_state(self) -> dict[str, object]:
        snapshot = self.current_state.snapshot()
        return {"revision": snapshot.revision, "slots": [asdict(slot) for slot in snapshot.slots]}

    def dashboard_overview(self) -> dict[str, object]:
        counts = {
            "conversations": int(
                self._db.execute(
                    "SELECT COUNT(*) FROM conversation_episodes"
                ).fetchone()[0]
            ),
            "messages": int(
                self._db.execute(
                    """SELECT COUNT(*) FROM messages
                       WHERE role IN ('user', 'event') OR delivery_state IN
                           ('delivered', 'uncertain', 'internal')"""
                ).fetchone()[0]
            ),
            "reflections": int(
                self._db.execute(
                    "SELECT COUNT(*) FROM reflections WHERE state='completed'"
                ).fetchone()[0]
            ),
            "goals": int(
                self._db.execute(
                    """SELECT COUNT(*) FROM goals
                       WHERE status IN ('active', 'waiting', 'blocked')"""
                ).fetchone()[0]
            ),
            "emotions": int(
                self._db.execute("SELECT COUNT(*) FROM emotions").fetchone()[0]
            ),
            "memories": int(
                self._db.execute(
                    """SELECT COUNT(*) FROM memories AS m
                       WHERE m.superseded_by IS NULL
                         AND (m.expires_at IS NULL OR m.expires_at > ?)
                         AND NOT EXISTS (
                             SELECT 1 FROM memory_tombstones AS t
                             WHERE t.kind=m.kind AND t.key=m.key AND t.scope_key=m.scope_key
                         )""",
                    (time.time(),),
                ).fetchone()[0]
            ),
        }
        latest_message = self._db.execute(
            """SELECT MAX(created_at) FROM messages
               WHERE role IN ('user', 'event') OR delivery_state IN
                   ('delivered', 'uncertain', 'internal')"""
        ).fetchone()[0]
        state = self.self_state()
        waiting = bool(str(state.get("pending_reply_expectation") or "").strip())
        return {
            "counts": counts,
            "current_state": self.dashboard_current_state(),
            "mood": {
                "state": state["mood_state"],
                "intensity": state["mood_intensity"],
                "cause": state["mood_cause"],
                "updated_at": _dashboard_unix(state["mood_updated_at"]),
            },
            "activity": {
                "name": state["activity"],
                "result": state.get("activity_result") or "",
                "since": state["activity_since"],
                "since_timestamp": self.context_timestamp(state["activity_since"]),
            },
            "heartbeat": {
                "next_at": _dashboard_unix(state.get("next_heartbeat_at")),
                "last_at": _dashboard_unix(state.get("last_heartbeat_at")),
                "running": state.get("heartbeat_claimed_at") is not None,
                "kind": state.get("heartbeat_claim_kind"),
                "reply_check_at": (
                    _dashboard_unix(state.get("pending_reply_next_check_at"))
                    if waiting
                    else None
                ),
            },
            "latest_message_at": latest_message,
            "latest_message_timestamp": (
                self.context_timestamp(latest_message) if latest_message is not None else None
            ),
            "usage": self.dashboard_usage(days=30),
        }

    def list_memories(self, limit: int = 200) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        self.memories.repository.purge_expired()
        now = time.time()
        rows = self._db.execute(
            """SELECT id, kind, key, scope_key, content, activation, authority,
                      evidence_quote, importance, created_at, updated_at,
                      expires_at
               FROM memories AS m
               WHERE m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key AND t.scope_key=m.scope_key
                 )
               ORDER BY CASE m.activation
                          WHEN 'always' THEN 0
                          WHEN 'recent' THEN 1
                          WHEN 'recall' THEN 2
                          WHEN 'scoped' THEN 3
                          ELSE 4
                        END,
                        m.updated_at DESC, m.id DESC
               LIMIT ?""",
            (now, limit),
        ).fetchall()
        results: list[dict[str, object]] = []
        scoped_goal_ids = {row["scope_key"][5:] for row in rows if row["scope_key"].startswith("goal:")}
        goal_titles = {}
        if scoped_goal_ids:
            placeholders = ",".join("?" for _ in scoped_goal_ids)
            goal_titles = {
                str(goal["id"]): str(goal["title"])
                for goal in self._db.execute(
                    f"SELECT id, title FROM goals WHERE id IN ({placeholders})",
                    tuple(scoped_goal_ids),
                )
            }
        for row in rows:
            item = self._memory_public_dict(row)
            if item["activation"] == "scoped":
                scope = item["scope"]
                item["scope_label"] = (
                    f"Goal · {goal_titles.get(scope[5:], scope[5:])}" if scope.startswith("goal:")
                    else {"heartbeat": "心跳", "webhook": "Webhook"}.get(scope, scope)
                )
            results.append(item)
        return results

    def _memory_public_dict(self, row: sqlite3.Row) -> dict[str, object]:
        item = dict(row)
        item["evidence"] = item.pop("evidence_quote")
        item["scope"] = item.pop("scope_key")
        add_context_timestamps(
            item, ("created_at", "updated_at", "expires_at"), self._timezone
        )
        return item

    def _active_memory_row(self, memory_id: int) -> sqlite3.Row | None:
        return self._db.execute(
            """SELECT id, kind, key, scope_key, content, activation, authority,
                      evidence_quote, importance, created_at, updated_at,
                      expires_at
               FROM memories AS m
               WHERE m.id=?
                 AND m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key AND t.scope_key=m.scope_key
                 )""",
            (memory_id, time.time()),
        ).fetchone()

    def update_memory_content(
        self, memory_id: int, content: str
    ) -> dict[str, object] | None:
        text = content.strip()
        if not text or len(text) > 2000:
            raise ValueError("content must contain between 1 and 2000 characters")
        now = time.time()
        with self._db:
            row = self._active_memory_row(memory_id)
            if row is None:
                return None
            self._db.execute(
                "UPDATE memories SET content=?, updated_at=? WHERE id=?",
                (text, now, memory_id),
            )
        updated = self._active_memory_row(memory_id)
        return self._memory_public_dict(updated) if updated else None

    def forget_memory_by_id(self, memory_id: int, reason: str) -> bool:
        text = reason.strip() or "Deleted from dashboard"
        if len(text) > 500:
            raise ValueError("reason must contain at most 500 characters")
        now = time.time()
        with self._db:
            row = self._active_memory_row(memory_id)
            if row is None:
                return False
            self.memories.repository.forget(
                {**dict(row), "meta": {"scope": row["scope_key"]}},
                {"event_id": "dashboard:forget", "quote": text}, now=now,
            )
        return True
