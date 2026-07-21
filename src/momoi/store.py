import json
import hashlib
import logging
import math
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .channel import (
    ChannelMessage,
    media_path,
    normalize_channel_message,
    render_channel_message,
)
from .config import HeartbeatConfig, NotificationConfig
from .emotions import emotion_slug, valid_emotion_slug
from .models import (
    AgentReply,
    IncomingMessage,
    MemoryCandidate,
    MemoryConflictCandidate,
    MemoryForgetCandidate,
    OutboxMessage,
    TurnDraft,
)


logger = logging.getLogger(__name__)


MEMORY_KINDS = {"profile", "preference", "relationship", "shared", "episodic", "routine"}
MOOD_STATES = {
    "cheerful",
    "excited",
    "calm",
    "focused",
    "tired",
    "down",
    "frustrated",
}
BASELINE_MOOD_STATE = "calm"
BASELINE_MOOD_INTENSITY = 0.35
BASELINE_MOOD_CAUSE = "resting baseline"
DEFAULT_ACTIVITY = "spending time freely"
CJK_STOP_CHARS = set("的了是在我你他她它们和就都也很还把被让要会呢吧啊哦呀")


def estimate_tokens(text: str) -> int:
    ascii_chars = sum(ord(char) < 128 for char in text)
    return max(1, math.ceil((len(text) - ascii_chars) + ascii_chars / 4))


def lexical_units(text: str) -> set[str]:
    normalized = text.casefold()
    units = set(re.findall(r"[a-z0-9_]{2,}", normalized))
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        units.update(char for char in run if char not in CJK_STOP_CHARS)
        if len(run) == 1:
            units.add(run)
        else:
            units.update(run[index : index + 2] for index in range(len(run) - 1))
    return units


class Store:
    def __init__(self, path: Path, workspace: Path | None = None) -> None:
        database = Path(path).expanduser().resolve()
        self._workspace = (workspace or database.parent).expanduser().resolve()
        self._db = sqlite3.connect(database)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self._migrate_emotion_paths()
        self._recover_outbox()
        self._recover_webhooks()

    def close(self) -> None:
        self._db.close()

    def _migrate(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                occurred_at REAL NOT NULL,
                received_at REAL NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '',
                processed INTEGER NOT NULL DEFAULT 0 CHECK (processed IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                source_event_ids_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE,
                text TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                possible_duplicate INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT,
                kind TEXT NOT NULL DEFAULT 'text',
                media_path TEXT,
                payload_json TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS continuity_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                content TEXT NOT NULL,
                source_event_ids_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_message_id INTEGER NOT NULL,
                end_message_id INTEGER NOT NULL UNIQUE,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                content TEXT NOT NULL,
                authority TEXT NOT NULL CHECK (authority = 'owner'),
                source_event_id TEXT NOT NULL,
                evidence_quote TEXT NOT NULL,
                importance REAL NOT NULL DEFAULT 0.5,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                expires_at REAL,
                superseded_by INTEGER
            );
            CREATE INDEX IF NOT EXISTS memories_active
                ON memories(kind, key) WHERE superseded_by IS NULL;
            CREATE TABLE IF NOT EXISTS memory_tombstones (
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                evidence_quote TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (kind, key)
            );
            CREATE TABLE IF NOT EXISTS memory_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id INTEGER NOT NULL,
                source_event_id TEXT NOT NULL,
                quote TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(memory_id, source_event_id, quote)
            );
            CREATE TABLE IF NOT EXISTS memory_conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                existing_memory_id INTEGER NOT NULL,
                candidate_content TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                evidence_quote TEXT NOT NULL,
                importance REAL NOT NULL DEFAULT 0.5,
                status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
                resolution TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS memory_conflicts_open
                ON memory_conflicts(kind, key) WHERE status='open';
            CREATE TABLE IF NOT EXISTS goals (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                success_criteria TEXT NOT NULL,
                authority TEXT NOT NULL CHECK (authority IN ('owner', 'agent')),
                source_event_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('active', 'waiting', 'blocked', 'done', 'cancelled')
                ),
                plan_json TEXT NOT NULL,
                next_action TEXT NOT NULL DEFAULT '',
                waiting_for TEXT NOT NULL DEFAULT '',
                blocked_reason TEXT NOT NULL DEFAULT '',
                latest_result TEXT NOT NULL DEFAULT '',
                schedule_json TEXT NOT NULL DEFAULT '',
                next_review_at REAL,
                retry_at REAL,
                failure_count INTEGER NOT NULL DEFAULT 0,
                review_claimed_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS goals_due
                ON goals(next_review_at) WHERE status IN ('active', 'waiting');
            CREATE TABLE IF NOT EXISTS reminders (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'fired', 'cancelled')),
                fire_at REAL NOT NULL,
                schedule_json TEXT NOT NULL DEFAULT '',
                claimed_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS reminders_due
                ON reminders(fire_at) WHERE status='pending';
            CREATE TABLE IF NOT EXISTS self_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                mood_state TEXT NOT NULL,
                mood_intensity REAL NOT NULL,
                mood_cause TEXT NOT NULL,
                mood_updated_at REAL NOT NULL,
                mood_settle_at REAL,
                activity TEXT NOT NULL,
                activity_since REAL NOT NULL,
                last_heartbeat_at REAL,
                next_heartbeat_at REAL,
                heartbeat_claimed_at REAL,
                heartbeat_day TEXT NOT NULL DEFAULT '',
                heartbeat_count INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY,
                turn_id TEXT NOT NULL UNIQUE,
                goal_id TEXT NOT NULL,
                notification_key TEXT NOT NULL,
                priority TEXT NOT NULL CHECK (priority IN ('normal', 'urgent')),
                reason TEXT NOT NULL,
                messages_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('pending', 'queued')),
                not_before REAL NOT NULL,
                claimed_at REAL,
                created_at REAL NOT NULL,
                queued_at REAL
            );
            CREATE INDEX IF NOT EXISTS notifications_due
                ON notifications(not_before) WHERE state='pending';
            CREATE TABLE IF NOT EXISTS emotions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                path TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tool_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                capability TEXT NOT NULL DEFAULT 'external_effect',
                arguments_sha256 TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('dispatching', 'completed')),
                result_json TEXT,
                ok INTEGER,
                started_at REAL NOT NULL,
                completed_at REAL,
                UNIQUE(turn_id, tool_call_id)
            );
            CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('owner', 'autonomous')),
                source_ids_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('running', 'needs_reconciliation', 'completed', 'cancelled')
                ),
                external_effect_started INTEGER NOT NULL DEFAULT 0
                    CHECK (external_effect_started IN (0, 1)),
                stage TEXT NOT NULL DEFAULT 'started',
                failure_reason TEXT,
                llm_calls INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliations (
                turn_id TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK (status IN ('open', 'resolved', 'resumed')),
                reason TEXT NOT NULL,
                resolution TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS turn_progress (
                turn_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                part_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (turn_id, tool_call_id, part_index)
            );
            CREATE TABLE IF NOT EXISTS webhook_runs (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                idempotency_key TEXT,
                plan_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('queued', 'running', 'waiting_delivery',
                              'succeeded', 'failed', 'ambiguous')
                ),
                current_step INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(workflow_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS webhook_runs_ready
                ON webhook_runs(state, created_at);
            CREATE TABLE IF NOT EXISTS webhook_steps (
                run_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                step_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('message', 'exec')),
                state TEXT NOT NULL CHECK (
                    state IN ('queued', 'running', 'waiting_delivery',
                              'succeeded', 'failed', 'ambiguous')
                ),
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                started_at REAL,
                completed_at REAL,
                PRIMARY KEY (run_id, step_index),
                FOREIGN KEY (run_id) REFERENCES webhook_runs(id) ON DELETE CASCADE
            );
            """
        )
        outbox_columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(outbox)").fetchall()
        }
        if "kind" not in outbox_columns:
            self._db.execute(
                "ALTER TABLE outbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'"
            )
        if "media_path" not in outbox_columns:
            self._db.execute("ALTER TABLE outbox ADD COLUMN media_path TEXT")
        if "payload_json" not in outbox_columns:
            self._db.execute(
                "ALTER TABLE outbox ADD COLUMN payload_json TEXT NOT NULL DEFAULT ''"
            )
        event_columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(events)").fetchall()
        }
        if "payload_json" not in event_columns:
            self._db.execute(
                "ALTER TABLE events ADD COLUMN payload_json TEXT NOT NULL DEFAULT ''"
            )
        turn_columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(turns)").fetchall()
        }
        if "stage" not in turn_columns:
            self._db.execute(
                "ALTER TABLE turns ADD COLUMN stage TEXT NOT NULL DEFAULT 'started'"
            )
        if "failure_reason" not in turn_columns:
            self._db.execute("ALTER TABLE turns ADD COLUMN failure_reason TEXT")
        for name in ("llm_calls", "input_tokens", "output_tokens"):
            if name not in turn_columns:
                self._db.execute(
                    f"ALTER TABLE turns ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                )
        tool_audit_columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(tool_audit)").fetchall()
        }
        if "capability" not in tool_audit_columns:
            self._db.execute(
                """ALTER TABLE tool_audit ADD COLUMN capability TEXT NOT NULL
                   DEFAULT 'external_effect'"""
            )
        goal_columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(goals)").fetchall()
        }
        if "schedule_json" not in goal_columns:
            self._db.execute(
                "ALTER TABLE goals ADD COLUMN schedule_json TEXT NOT NULL DEFAULT ''"
            )
        if "retry_at" not in goal_columns:
            self._db.execute("ALTER TABLE goals ADD COLUMN retry_at REAL")
        if "failure_count" not in goal_columns:
            self._db.execute(
                "ALTER TABLE goals ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0"
            )
        reminder_columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(reminders)").fetchall()
        }
        if "schedule_json" not in reminder_columns:
            self._db.execute(
                "ALTER TABLE reminders ADD COLUMN schedule_json TEXT NOT NULL DEFAULT ''"
            )
        now = time.time()
        self._db.execute(
            """INSERT OR IGNORE INTO self_state
               (id, mood_state, mood_intensity, mood_cause, mood_updated_at,
                activity, activity_since, updated_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?)""",
            (
                BASELINE_MOOD_STATE,
                BASELINE_MOOD_INTENSITY,
                BASELINE_MOOD_CAUSE,
                now,
                DEFAULT_ACTIVITY,
                now,
                now,
            ),
        )
        self._db.execute(
            """UPDATE self_state SET mood_state=?, mood_intensity=?, mood_cause=?
               WHERE mood_state='cheerful' AND mood_intensity=0.55
                 AND mood_cause='personality baseline' AND mood_settle_at IS NULL""",
            (
                BASELINE_MOOD_STATE,
                BASELINE_MOOD_INTENSITY,
                BASELINE_MOOD_CAUSE,
            ),
        )
        self._db.execute(
            "UPDATE self_state SET activity=? WHERE activity='自由安排自己的时间'",
            (DEFAULT_ACTIVITY,),
        )
        self._db.execute(
            """INSERT OR IGNORE INTO memory_evidence
               (memory_id, source_event_id, quote, created_at)
               SELECT id, source_event_id, evidence_quote, created_at FROM memories"""
        )
        legacy_summary = self._db.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='conversation_summary'"""
        ).fetchone()
        if legacy_summary:
            self._db.execute(
                """INSERT OR IGNORE INTO conversation_summaries
                   (start_message_id, end_message_id, content, created_at)
                   SELECT COALESCE((SELECT MIN(id) FROM messages), 1),
                          through_message_id, content, updated_at
                   FROM conversation_summary WHERE id=1"""
            )
            self._db.execute("DROP TABLE conversation_summary")
        self._db.execute("UPDATE goals SET review_claimed_at=NULL")
        self._db.execute("UPDATE reminders SET claimed_at=NULL WHERE status='pending'")
        self._db.execute("UPDATE notifications SET claimed_at=NULL WHERE state='pending'")
        self._db.execute("UPDATE self_state SET heartbeat_claimed_at=NULL WHERE id=1")
        self._db.commit()

    def _recover_outbox(self) -> None:
        self._db.execute(
            """UPDATE outbox
               SET state='ambiguous', possible_duplicate=1, next_attempt_at=0
               WHERE state='sending' AND attempts < 2"""
        )
        self._db.execute(
            "UPDATE outbox SET state='failed' WHERE state='sending' AND attempts >= 2"
        )
        self._db.commit()

    def _recover_webhooks(self) -> None:
        now = time.time()
        with self._db:
            running_execs = self._db.execute(
                "SELECT run_id, step_index FROM webhook_steps WHERE kind='exec' AND state='running'"
            ).fetchall()
            for row in running_execs:
                self._db.execute(
                    """UPDATE webhook_steps SET state='ambiguous',
                       error='process_interrupted', completed_at=?
                       WHERE run_id=? AND step_index=?""",
                    (now, row["run_id"], row["step_index"]),
                )
                self._db.execute(
                    """UPDATE webhook_runs SET state='ambiguous',
                       error='process_interrupted', updated_at=? WHERE id=?""",
                    (now, row["run_id"]),
                )
            self._db.execute(
                "UPDATE webhook_steps SET state='queued', started_at=NULL WHERE kind='message' AND state='running'"
            )
            self._db.execute(
                """UPDATE webhook_runs SET state='queued', updated_at=?
                   WHERE state='running' AND id NOT IN (
                       SELECT run_id FROM webhook_steps WHERE state='ambiguous'
                   )""",
                (now,),
            )

    def add_event(self, message: IncomingMessage) -> bool:
        payload = {
            "channel": message.channel,
            "segments": message.segments,
        }
        cursor = self._db.execute(
            """INSERT OR IGNORE INTO events
               (id, message_id, kind, content, occurred_at, received_at, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                message.event_id,
                message.message_id,
                f"{message.channel}.message",
                message.text,
                message.occurred_at,
                message.received_at,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        self._db.commit()
        return cursor.rowcount == 1

    def pending_events(self) -> list[IncomingMessage]:
        rows = self._db.execute(
            "SELECT * FROM events WHERE processed=0 ORDER BY received_at, rowid"
        ).fetchall()
        return [
            self._incoming_message(row)
            for row in rows
        ]

    @staticmethod
    def _incoming_message(row: sqlite3.Row) -> IncomingMessage:
        raw = str(row["payload_json"] or "")
        try:
            value = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            value = []
        if isinstance(value, dict):
            channel = str(value.get("channel") or "unknown")
            raw_segments = value.get("segments") or []
        else:
            kind = str(row["kind"] or "")
            channel = kind.removesuffix(".message") or "unknown"
            raw_segments = value if isinstance(value, list) else []
        segments = tuple(item for item in raw_segments if isinstance(item, dict))
        if not segments and row["content"]:
            segments = ({"type": "text", "data": {"text": row["content"]}},)
        return IncomingMessage(
            event_id=row["id"],
            message_id=row["message_id"],
            text=row["content"],
            occurred_at=row["occurred_at"],
            received_at=row["received_at"],
            segments=segments,
            channel=channel,
        )

    def discard_events(self, events: list[IncomingMessage]) -> None:
        with self._db:
            self._db.executemany(
                "UPDATE events SET processed=1 WHERE id=?",
                ((event.event_id,) for event in events),
            )

    def begin_turn(self, turn_id: str, kind: str, source_ids: list[str]) -> str:
        now = time.time()
        with self._db:
            row = self._db.execute(
                "SELECT state, external_effect_started FROM turns WHERE id=?",
                (turn_id,),
            ).fetchone()
            if row is None:
                self._db.execute(
                    """INSERT INTO turns
                       (id, kind, source_ids_json, state, started_at, updated_at)
                       VALUES (?, ?, ?, 'running', ?, ?)""",
                    (turn_id, kind, json.dumps(source_ids), now, now),
                )
                return "running"
            if row["state"] == "running" and row["external_effect_started"]:
                self._db.execute(
                    """UPDATE turns SET state='needs_reconciliation',
                       stage='needs_reconciliation',
                       failure_reason='process_interrupted_after_external_effect',
                       updated_at=? WHERE id=?""",
                    (now, turn_id),
                )
                self._open_reconciliation(
                    turn_id, "process_interrupted_after_external_effect", now
                )
                return "needs_reconciliation"
            return str(row["state"])

    def cancel_turn(
        self, turn_id: str, events: list[IncomingMessage] | None = None
    ) -> None:
        with self._db:
            row = self._db.execute(
                "SELECT external_effect_started FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            self._db.execute(
                """UPDATE turns SET state='cancelled', stage='cancelled',
                   failure_reason='owner_stop', updated_at=? WHERE id=?""",
                (time.time(), turn_id),
            )
            if row is not None and row["external_effect_started"]:
                self._open_reconciliation(
                    turn_id, "owner_stopped_after_external_effect", time.time()
                )
            self._db.executemany(
                "UPDATE events SET processed=1 WHERE id=?",
                ((event.event_id,) for event in events or []),
            )

    def record_turn_failure(self, turn_id: str, reason: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE turns SET failure_reason=?, updated_at=? WHERE id=?",
                (reason[:500], time.time(), turn_id),
            )

    def turn_has_external_effect(self, turn_id: str) -> bool:
        row = self._db.execute(
            "SELECT external_effect_started FROM turns WHERE id=?", (turn_id,)
        ).fetchone()
        return bool(row and row["external_effect_started"])

    def turn_usage(self, turn_id: str) -> dict[str, float | int]:
        row = self._db.execute(
            """SELECT started_at, llm_calls, input_tokens, output_tokens
               FROM turns WHERE id=?""",
            (turn_id,),
        ).fetchone()
        if row is None:
            return {"started_at": time.time(), "llm_calls": 0, "input": 0, "output": 0}
        return {
            "started_at": float(row["started_at"]),
            "llm_calls": int(row["llm_calls"]),
            "input": int(row["input_tokens"]),
            "output": int(row["output_tokens"]),
        }

    def record_turn_usage(
        self, turn_id: str, input_tokens: int, output_tokens: int
    ) -> None:
        with self._db:
            self._db.execute(
                """UPDATE turns SET llm_calls=llm_calls+1,
                   input_tokens=input_tokens+?, output_tokens=output_tokens+?,
                   updated_at=? WHERE id=?""",
                (
                    max(0, input_tokens),
                    max(0, output_tokens),
                    time.time(),
                    turn_id,
                ),
            )

    def open_reconciliation(self, turn_id: str, reason: str) -> None:
        with self._db:
            self._open_reconciliation(turn_id, reason, time.time())

    def _open_reconciliation(self, turn_id: str, reason: str, now: float) -> None:
        self._db.execute(
            """INSERT INTO reconciliations
               (turn_id, status, reason, resolution, created_at, updated_at)
               VALUES (?, 'open', ?, '', ?, ?)
               ON CONFLICT(turn_id) DO UPDATE SET status='open', reason=excluded.reason,
                 resolution='', updated_at=excluded.updated_at""",
            (turn_id, reason[:500], now, now),
        )

    def open_reconciliations_context(self) -> str:
        rows = self._db.execute(
            """SELECT turn_id, reason, created_at FROM reconciliations
               WHERE status='open' ORDER BY created_at LIMIT 10"""
        ).fetchall()
        return "\n".join(
            f"- turn_id={row['turn_id']} reason={row['reason']}"
            for row in rows
        )

    def resolve_reconciliation(
        self, turn_prefix: str, resolution: str, *, resume: bool
    ) -> dict[str, object]:
        prefix = turn_prefix.strip()
        resolution = resolution.strip()
        if len(prefix) < 8 or not re.fullmatch(r"[0-9a-f]+", prefix):
            raise ValueError("turn id prefix must contain at least 8 hexadecimal characters")
        if not resolution:
            raise ValueError("resolution is required")
        rows = self._db.execute(
            """SELECT * FROM reconciliations
               WHERE status='open' AND turn_id LIKE ? ORDER BY created_at""",
            (f"{prefix}%",),
        ).fetchall()
        if not rows:
            raise ValueError("open reconciliation not found")
        if len(rows) > 1:
            raise ValueError("turn id prefix is ambiguous")
        row = rows[0]
        status = "resumed" if resume else "resolved"
        with self._db:
            self._db.execute(
                """UPDATE reconciliations SET status=?, resolution=?, updated_at=?
                   WHERE turn_id=?""",
                (status, resolution[:2000], time.time(), row["turn_id"]),
            )
        return {
            **dict(row),
            "status": status,
            "resolution": resolution[:2000],
        }

    def history(self, token_budget: int, min_turns: int) -> list[dict[str, str]]:
        if token_budget <= 0:
            return []
        summary = self._db.execute(
            "SELECT MAX(end_message_id) AS end_id FROM conversation_summaries"
        ).fetchone()
        after_id = int(summary["end_id"]) if summary and summary["end_id"] else 0
        selected: list[sqlite3.Row] = []
        tokens = 0
        user_turns = 0
        rows = self._db.execute(
            "SELECT role, content FROM messages WHERE id>? ORDER BY id DESC",
            (after_id,),
        )
        for row in rows:
            row_tokens = estimate_tokens(row["content"])
            if selected and tokens + row_tokens > token_budget and user_turns >= min_turns:
                break
            selected.append(row)
            tokens += row_tokens
            if row["role"] == "user":
                user_turns += 1
        return [
            {"role": row["role"], "content": row["content"]}
            for row in reversed(selected)
        ]

    def summary_context(self, query: str, max_results: int, token_budget: int) -> str:
        rows = self.search_conversation_summaries(query, max_results)
        lines: list[str] = []
        tokens = 0
        for row in rows:
            line = (
                f"- [conversation:{row['id']} messages "
                f"{row['start_message_id']}-{row['end_message_id']}] {row['content']}"
            )
            line_tokens = estimate_tokens(line)
            if lines and tokens + line_tokens > token_budget:
                break
            lines.append(line)
            tokens += line_tokens
        return "\n".join(lines)

    def search_conversation_summaries(
        self, query: str, max_results: int
    ) -> list[dict[str, object]]:
        if max_results <= 0:
            return []
        rows = self._db.execute(
            """SELECT id, start_message_id, end_message_id, content, created_at
               FROM conversation_summaries ORDER BY end_message_id DESC"""
        ).fetchall()
        if not rows:
            return []
        query_units = lexical_units(query)
        latest_id = int(rows[0]["id"])
        ranked: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            units = lexical_units(str(row["content"]))
            overlap = len(query_units & units)
            latest = int(row["id"]) == latest_id
            if not latest and overlap == 0:
                continue
            score = overlap / max(1, math.sqrt(len(query_units) * len(units)))
            ranked.append((score + (0.15 if latest else 0), row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [dict(row) for _, row in ranked[:max_results]]

    def conversation_segment(self, segment_id: int) -> dict[str, object] | None:
        segment = self._db.execute(
            "SELECT * FROM conversation_summaries WHERE id=?", (segment_id,)
        ).fetchone()
        if segment is None:
            return None
        messages = self._db.execute(
            """SELECT id, role, content, created_at FROM messages
               WHERE id BETWEEN ? AND ? ORDER BY id LIMIT 200""",
            (segment["start_message_id"], segment["end_message_id"]),
        ).fetchall()
        selected: list[dict[str, object]] = []
        characters = 0
        for message in messages:
            content = str(message["content"])
            if selected and characters + len(content) > 30000:
                break
            selected.append(dict(message))
            characters += len(content)
        return {
            **dict(segment),
            "messages": selected,
            "truncated": len(selected) < len(messages),
        }

    def compaction_candidate(
        self, token_budget: int, min_turns: int
    ) -> tuple[list[dict[str, object]], int, int] | None:
        summary = self._db.execute(
            "SELECT MAX(end_message_id) AS end_id FROM conversation_summaries"
        ).fetchone()
        after_id = int(summary["end_id"]) if summary and summary["end_id"] else 0
        rows = self._db.execute(
            "SELECT id, role, content FROM messages WHERE id>? ORDER BY id",
            (after_id,),
        ).fetchall()
        tokens = 0
        user_turns = 0
        keep_from = len(rows)
        for index in range(len(rows) - 1, -1, -1):
            row_tokens = estimate_tokens(str(rows[index]["content"]))
            if (
                keep_from < len(rows)
                and tokens + row_tokens > token_budget
                and user_turns >= min_turns
            ):
                break
            keep_from = index
            tokens += row_tokens
            if rows[index]["role"] == "user":
                user_turns += 1
        while 0 < keep_from < len(rows) and rows[keep_from]["role"] != "user":
            keep_from -= 1
        compact = rows[:keep_from]
        if not compact:
            return None
        return (
            [dict(row) for row in compact],
            int(compact[0]["id"]),
            int(compact[-1]["id"]),
        )

    def save_conversation_summary(
        self, content: str, start_message_id: int, end_message_id: int
    ) -> None:
        with self._db:
            self._db.execute(
                """INSERT OR IGNORE INTO conversation_summaries
                   (start_message_id, end_message_id, content, created_at)
                   VALUES (?, ?, ?, ?)""",
                (start_message_id, end_message_id, content[:6000], time.time()),
            )

    def continuity(self) -> str:
        row = self._db.execute(
            "SELECT content FROM continuity_state WHERE id=1"
        ).fetchone()
        if row is None:
            return ""
        content = str(row["content"])
        try:
            state = json.loads(content)
        except json.JSONDecodeError:
            return content
        if not isinstance(state, dict):
            return content
        facts = state.get("short_term_facts")
        if isinstance(facts, list):
            now = time.time()
            state["short_term_facts"] = [
                fact
                for fact in facts
                if isinstance(fact, dict)
                and self._future_timestamp(fact.get("expires_at"), now)
            ]
        topic = state.get("topic")
        if not any(
            (
                isinstance(topic, dict) and bool(topic.get("text")),
                bool(state.get("open_loops")),
                bool(state.get("pending_commitments")),
                bool(state.get("short_term_facts")),
            )
        ):
            return ""
        return json.dumps(state, ensure_ascii=False, separators=(",", ":"))

    def continuity_context(self) -> str:
        """Return the writable projection shown to the model, without runtime metadata."""
        content = self.continuity()
        if not content:
            return ""
        try:
            state = json.loads(content)
        except json.JSONDecodeError:
            state = {"topic": content}
        if not isinstance(state, dict):
            return ""

        def text(value: object) -> str:
            if isinstance(value, dict):
                value = value.get("text")
            return str(value or "").strip()

        facts = []
        for fact in state.get("short_term_facts", []):
            if not isinstance(fact, dict):
                continue
            fact_text = text(fact)
            expires_at = fact.get("expires_at")
            if fact_text and isinstance(expires_at, str):
                facts.append({"text": fact_text, "expires_at": expires_at})
        projection = {
            "topic": text(state.get("topic")),
            "open_loops": [
                item
                for value in state.get("open_loops", [])
                if (item := text(value))
            ],
            "pending_commitments": [
                item
                for value in state.get("pending_commitments", [])
                if (item := text(value))
            ],
            "short_term_facts": facts,
        }
        return json.dumps(projection, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _future_timestamp(value: object, now: float) -> bool:
        if not isinstance(value, str):
            return False
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() > now
        except ValueError:
            return False

    @staticmethod
    def _serialize_continuity(
        value: dict[str, object] | str,
        event_ids: list[str],
        now: float,
    ) -> str:
        timestamp = datetime.fromtimestamp(now).astimezone().isoformat()

        def item(text: object) -> dict[str, object]:
            return {
                "text": str(text).strip()[:1000],
                "updated_at": timestamp,
                "source_event_ids": event_ids,
            }

        if isinstance(value, str):
            state: dict[str, object] = {
                "topic": item(value) if value.strip() else None,
                "open_loops": [],
                "pending_commitments": [],
                "short_term_facts": [],
            }
        else:
            topic = str(value.get("topic") or "").strip()
            state = {
                "topic": item(topic) if topic else None,
                "open_loops": [item(text) for text in value.get("open_loops", [])],
                "pending_commitments": [
                    item(text) for text in value.get("pending_commitments", [])
                ],
                "short_term_facts": [
                    {
                        **item(fact.get("text")),
                        "expires_at": str(fact.get("expires_at")),
                    }
                    for fact in value.get("short_term_facts", [])
                    if isinstance(fact, dict)
                ],
            }
        return json.dumps(state, ensure_ascii=False, separators=(",", ":"))

    def self_state(self, now: float | None = None) -> dict[str, object]:
        now = time.time() if now is None else now
        row = self._db.execute("SELECT * FROM self_state WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("self_state is not initialized")
        state = dict(row)
        settle_at = state.get("mood_settle_at")
        if settle_at is not None and float(settle_at) <= now:
            previous = str(state["mood_state"])
            with self._db:
                self._db.execute(
                    """UPDATE self_state
                       SET mood_state=?, mood_intensity=?, mood_cause=?, mood_updated_at=?,
                           mood_settle_at=NULL, updated_at=? WHERE id=1""",
                    (
                        BASELINE_MOOD_STATE,
                        BASELINE_MOOD_INTENSITY,
                        BASELINE_MOOD_CAUSE,
                        now,
                        now,
                    ),
                )
            state.update(
                mood_state=BASELINE_MOOD_STATE,
                mood_intensity=BASELINE_MOOD_INTENSITY,
                mood_cause=BASELINE_MOOD_CAUSE,
                mood_updated_at=now,
                mood_settle_at=None,
                updated_at=now,
            )
            logger.debug("Mood settled from=%s to=%s", previous, BASELINE_MOOD_STATE)
        return state

    def self_state_context(self, now: float | None = None) -> str:
        state = self.self_state(now)

        def timestamp(value: object) -> str | None:
            return (
                datetime.fromtimestamp(float(value)).astimezone().isoformat(
                    timespec="seconds"
                )
                if value is not None
                else None
            )

        return json.dumps(
            {
                "mood": {
                    "state": state["mood_state"],
                    "intensity": state["mood_intensity"],
                    "cause": state["mood_cause"],
                    "updated_at": timestamp(state["mood_updated_at"]),
                    "settle_at": timestamp(state["mood_settle_at"]),
                },
                "activity": {
                    "text": state["activity"],
                    "since": timestamp(state["activity_since"]),
                },
                "last_heartbeat_at": timestamp(state["last_heartbeat_at"]),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _apply_mood_transition(
        self, transition: dict[str, object] | None, now: float
    ) -> None:
        if transition is None:
            return
        previous = self._db.execute(
            "SELECT mood_state, mood_intensity FROM self_state WHERE id=1"
        ).fetchone()
        duration = int(transition["duration_minutes"])
        self._db.execute(
            """UPDATE self_state
               SET mood_state=?, mood_intensity=?, mood_cause=?,
                   mood_updated_at=?, mood_settle_at=?, updated_at=? WHERE id=1""",
            (
                transition["state"],
                transition["intensity"],
                str(transition["cause"])[:300],
                now,
                now + duration * 60,
                now,
            ),
        )
        logger.debug(
            "Mood changed from=%s to=%s intensity=%.2f duration_minutes=%d cause=%s",
            previous["mood_state"] if previous else "unknown",
            transition["state"],
            float(transition["intensity"]),
            duration,
            str(transition["cause"]).replace("\n", " ")[:300],
        )

    def ensure_heartbeat(self, config: HeartbeatConfig, now: float | None = None) -> None:
        if not config.enabled:
            return
        now = time.time() if now is None else now
        with self._db:
            self._db.execute(
                """UPDATE self_state SET next_heartbeat_at=?, updated_at=?
                   WHERE id=1 AND next_heartbeat_at IS NULL""",
                (now + config.initial_delay_seconds, now),
            )

    def claim_due_heartbeat(
        self,
        config: HeartbeatConfig,
        notifications: NotificationConfig,
        now: float | None = None,
    ) -> dict[str, object] | None:
        if not config.enabled:
            return None
        now = time.time() if now is None else now
        with self._db:
            row = self._db.execute("SELECT * FROM self_state WHERE id=1").fetchone()
            if (
                row is None
                or row["heartbeat_claimed_at"] is not None
                or row["next_heartbeat_at"] is None
                or float(row["next_heartbeat_at"]) > now
            ):
                return None
            quiet_until = self._quiet_until(now, notifications)
            if quiet_until > now:
                self._db.execute(
                    """UPDATE self_state SET next_heartbeat_at=?, updated_at=?
                       WHERE id=1""",
                    (quiet_until, now),
                )
                return None
            day = datetime.fromtimestamp(
                now, ZoneInfo(notifications.timezone)
            ).date().isoformat()
            count = int(row["heartbeat_count"]) if row["heartbeat_day"] == day else 0
            if count >= config.max_daily_turns:
                _, next_day = self._local_day_bounds(now, notifications.timezone)
                next_at = self._quiet_until(next_day, notifications)
                self._db.execute(
                    """UPDATE self_state SET heartbeat_day=?, heartbeat_count=0,
                       next_heartbeat_at=?, updated_at=? WHERE id=1""",
                    (day, next_at, now),
                )
                return None
            self._db.execute(
                "UPDATE self_state SET heartbeat_claimed_at=? WHERE id=1",
                (now,),
            )
        claimed = dict(row)
        claimed["heartbeat_day"] = day
        claimed["heartbeat_count"] = count
        return claimed

    def next_heartbeat_due_at(self, enabled: bool) -> float | None:
        if not enabled:
            return None
        row = self._db.execute(
            """SELECT next_heartbeat_at FROM self_state
               WHERE id=1 AND heartbeat_claimed_at IS NULL"""
        ).fetchone()
        return (
            float(row["next_heartbeat_at"])
            if row and row["next_heartbeat_at"] is not None
            else None
        )

    def release_heartbeat_claim(self, delay_seconds: float) -> None:
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE self_state SET heartbeat_claimed_at=NULL,
                   next_heartbeat_at=?, updated_at=? WHERE id=1""",
                (now + delay_seconds, now),
            )

    def clear_heartbeat_claim(self) -> None:
        with self._db:
            self._db.execute(
                "UPDATE self_state SET heartbeat_claimed_at=NULL WHERE id=1"
            )

    def commit_heartbeat(
        self,
        turn_id: str,
        *,
        activity: str,
        next_heartbeat_at: float,
        mood_transition: dict[str, object] | None,
        messages: list[ChannelMessage],
        reason: str,
        timezone: str,
    ) -> None:
        now = time.time()
        day = datetime.fromtimestamp(now, ZoneInfo(timezone)).date().isoformat()
        current = self.self_state(now)
        count = int(current["heartbeat_count"]) if current["heartbeat_day"] == day else 0
        with self._db:
            self._apply_mood_transition(mood_transition, now)
            activity_since = (
                current["activity_since"]
                if current["activity"] == activity
                else now
            )
            self._db.execute(
                """UPDATE self_state SET activity=?, activity_since=?,
                   last_heartbeat_at=?, next_heartbeat_at=?, heartbeat_claimed_at=NULL,
                   heartbeat_day=?, heartbeat_count=?, updated_at=? WHERE id=1""",
                (
                    activity,
                    activity_since,
                    now,
                    next_heartbeat_at,
                    day,
                    count + 1,
                    now,
                ),
            )
            if messages:
                self._db.execute(
                    """INSERT OR IGNORE INTO notifications
                       (id, turn_id, goal_id, notification_key, priority, reason,
                        messages_json, state, not_before, created_at)
                       VALUES (?, ?, 'heartbeat', 'heartbeat.chat', 'normal', ?, ?,
                               'pending', ?, ?)""",
                    (
                        f"notification:{turn_id}",
                        turn_id,
                        reason[:500],
                        json.dumps(messages, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   failure_reason=NULL, updated_at=? WHERE id=?""",
                (now, turn_id),
            )

    def goal(self, goal_id: str) -> dict[str, object] | None:
        row = self._db.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        return self._goal_dict(row) if row else None

    def list_goals(self, include_closed: bool = False) -> list[dict[str, object]]:
        where = "" if include_closed else "WHERE status IN ('active', 'waiting', 'blocked')"
        rows = self._db.execute(
            f"SELECT * FROM goals {where} ORDER BY updated_at DESC"
        ).fetchall()
        return [self._goal_dict(row) for row in rows]

    def commit_goal_draft(self, draft: TurnDraft) -> None:
        with self._db:
            self._apply_goal_mutations(draft, time.time())

    def active_goals_context(self) -> str:
        rows = self._db.execute(
            """SELECT * FROM goals
               WHERE status IN ('active', 'waiting', 'blocked')
               ORDER BY COALESCE(next_review_at, 1e30), updated_at DESC
               LIMIT 20"""
        ).fetchall()
        if not rows:
            return ""
        lines = []
        for row in rows:
            goal = self._goal_dict(row)
            review = (
                datetime.fromtimestamp(float(goal["next_review_at"])).astimezone().isoformat(timespec="seconds")
                if goal["next_review_at"] is not None
                else "none"
            )
            lines.append(
                f"- id={goal['id']} status={goal['status']} title={goal['title']} "
                f"next_action={goal['next_action'] or 'none'} next_review_at={review} "
                f"retry_at={goal.get('retry_at') or 'none'} "
                f"schedule={json.dumps(goal['schedule'], ensure_ascii=False) if goal['schedule'] else 'none'}"
            )
        return "\n".join(lines)

    def active_reminders_context(self) -> str:
        rows = self._db.execute(
            """SELECT id, text, fire_at, schedule_json FROM reminders
               WHERE status='pending' ORDER BY fire_at LIMIT 20"""
        ).fetchall()
        return "\n".join(
            f"- id={row['id']} fire_at="
            f"{datetime.fromtimestamp(row['fire_at']).astimezone().isoformat(timespec='seconds')} "
            f"schedule={row['schedule_json'] or 'none'} text={row['text']}"
            for row in rows
        )

    def add_emotion(self, slug: str, path: str | Path, description: str) -> dict[str, object]:
        slug = slug.strip()
        description = description.strip()
        asset = self._resolve_asset_path(path)
        if not valid_emotion_slug(slug):
            raise ValueError("slug must use lowercase letters, digits, dot, underscore, or hyphen")
        if not asset.is_file():
            raise ValueError("path must be an existing file")
        if not description or len(description) > 500:
            raise ValueError("description must contain 1 to 500 characters")
        now = time.time()
        with self._db:
            self._db.execute(
                """INSERT INTO emotions(slug, path, description, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET path=excluded.path,
                     description=excluded.description, updated_at=excluded.updated_at""",
                (slug, self._stored_asset_path(asset), description, now, now),
            )
        return self.emotion(slug) or {}

    def delete_emotion(self, slug: str) -> bool:
        with self._db:
            cursor = self._db.execute("DELETE FROM emotions WHERE slug=?", (slug,))
        return cursor.rowcount == 1

    def emotion(self, slug: str) -> dict[str, object] | None:
        row = self._db.execute(
            "SELECT id, slug, path, description FROM emotions WHERE slug=?", (slug,)
        ).fetchone()
        return self._emotion_dict(row) if row else None

    def list_emotions(self) -> list[dict[str, object]]:
        rows = self._db.execute(
            "SELECT id, slug, path, description FROM emotions ORDER BY id"
        ).fetchall()
        return [self._emotion_dict(row) for row in rows]

    def _emotion_dict(self, row: sqlite3.Row) -> dict[str, object]:
        item = dict(row)
        item["path"] = str(self._resolve_asset_path(str(item["path"])))
        return item

    def _resolve_asset_path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self._workspace / path).resolve()

    def _stored_asset_path(self, value: str | Path) -> str:
        path = self._resolve_asset_path(value)
        try:
            return path.relative_to(self._workspace).as_posix()
        except ValueError:
            return str(path)

    def _migrate_emotion_paths(self) -> None:
        with self._db:
            rows = self._db.execute("SELECT path FROM emotions").fetchall()
            for row in rows:
                old = str(row["path"])
                path = Path(old).expanduser()
                candidate = self._resolve_asset_path(old)
                if path.is_absolute() and not candidate.is_file():
                    relocated = self._workspace / "emotion" / path.name
                    if relocated.is_file():
                        candidate = relocated
                if not candidate.is_file():
                    continue
                new = self._stored_asset_path(candidate)
                if new != old:
                    self._db.execute(
                        "UPDATE emotions SET path=? WHERE path=?", (new, old)
                    )
                    outbox = self._db.execute(
                        "SELECT id, payload_json FROM outbox WHERE media_path=?",
                        (old,),
                    ).fetchall()
                    for message in outbox:
                        payload = str(message["payload_json"] or "").replace(old, new)
                        self._db.execute(
                            "UPDATE outbox SET media_path=?, payload_json=? WHERE id=?",
                            (new, payload, message["id"]),
                        )
            failed = self._db.execute(
                """SELECT id, media_path FROM outbox
                   WHERE state='failed' AND kind='image'
                     AND last_error LIKE 'media asset cannot be read:%'"""
            ).fetchall()
            for message in failed:
                if message["media_path"] and self._resolve_asset_path(
                    str(message["media_path"])
                ).is_file():
                    self._db.execute(
                        """UPDATE outbox SET state='pending', attempts=0,
                           last_error=NULL, next_attempt_at=0 WHERE id=?""",
                        (message["id"],),
                    )

    def emotion_path_referenced(
        self, path: str, *, exclude_slug: str | None = None
    ) -> bool:
        path = self._stored_asset_path(path)
        return self._db.execute(
            """SELECT 1 FROM emotions
               WHERE path=? AND (? IS NULL OR slug<>?)
               UNION ALL
               SELECT 1 FROM outbox
               WHERE media_path=? AND state NOT IN ('sent', 'failed')
               UNION ALL
               SELECT 1 FROM notifications AS n
               JOIN emotions AS e ON e.path=?
               WHERE n.state='pending'
                 AND instr(n.messages_json, 'emotion://' || e.slug) > 0
               LIMIT 1""",
            (path, exclude_slug, exclude_slug, path, path),
        ).fetchone() is not None

    def emotion_context(self, token_budget: int = 4000) -> str:
        lines: list[str] = []
        tokens = 0
        for row in self.list_emotions():
            line = f"- slug={row['slug']} meaning={row['description']}"
            line_tokens = estimate_tokens(line)
            if tokens + line_tokens > token_budget:
                break
            lines.append(line)
            tokens += line_tokens
        return "\n".join(lines)

    def _outbox_content(
        self, message: ChannelMessage
    ) -> tuple[str, str, str | None, dict[str, object]]:
        slug = emotion_slug(message) if isinstance(message, str) else None
        if slug is not None:
            asset = self.emotion(slug)
            if asset is None:
                raise ValueError(f"unknown emotion slug: {slug}")
            path = str(asset["path"])
            path = self._stored_asset_path(path)
            payload: dict[str, object] = {
                "action": "message",
                "segments": [{"type": "image", "data": {"file": path}}],
            }
            return message, "image", path, payload
        payload = normalize_channel_message(message)
        text = message if isinstance(message, str) else render_channel_message(payload)
        if payload["action"] == "forward":
            kind = "forward"
        else:
            segments = payload.get("segments") or []
            kind = str(segments[0].get("type")) if len(segments) == 1 else "message"
        return text, kind, media_path(payload), payload

    def reminder(self, reminder_id: str) -> dict[str, object] | None:
        row = self._db.execute(
            "SELECT * FROM reminders WHERE id=?", (reminder_id,)
        ).fetchone()
        return self._reminder_dict(row) if row else None

    @staticmethod
    def _reminder_dict(row: sqlite3.Row) -> dict[str, object]:
        reminder = dict(row)
        schedule_json = str(reminder.pop("schedule_json", ""))
        reminder["schedule"] = json.loads(schedule_json) if schedule_json else None
        return reminder

    def claim_due_reminder(self) -> dict[str, object] | None:
        now = time.time()
        with self._db:
            row = self._db.execute(
                """SELECT * FROM reminders
                   WHERE status='pending' AND fire_at<=? AND claimed_at IS NULL
                   ORDER BY fire_at LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                "UPDATE reminders SET claimed_at=? WHERE id=?", (now, row["id"])
            )
        return self._reminder_dict(row)

    def next_reminder_due_at(self) -> float | None:
        row = self._db.execute(
            """SELECT MIN(fire_at) AS due FROM reminders
               WHERE status='pending' AND claimed_at IS NULL"""
        ).fetchone()
        return float(row["due"]) if row and row["due"] is not None else None

    def fire_reminder(
        self,
        reminder_id: str,
        config: NotificationConfig | None = None,
    ) -> bool:
        now = time.time()
        with self._db:
            row = self._db.execute(
                """SELECT text, fire_at, schedule_json FROM reminders
                   WHERE id=? AND status='pending'""",
                (reminder_id,),
            ).fetchone()
            if row is None:
                return False
            schedule = json.loads(str(row["schedule_json"])) if row["schedule_json"] else None
            if schedule is not None and config is not None:
                quiet_until = self._quiet_until(now, config)
                if quiet_until > now:
                    self._db.execute(
                        """UPDATE reminders SET fire_at=?, claimed_at=NULL, updated_at=?
                           WHERE id=?""",
                        (quiet_until, now, reminder_id),
                    )
                    return False
            occurrence = int(float(row["fire_at"]))
            if schedule is None:
                self._db.execute(
                    """UPDATE reminders SET status='fired', claimed_at=NULL, updated_at=?
                       WHERE id=?""",
                    (now, reminder_id),
                )
            else:
                self._db.execute(
                    """UPDATE reminders SET fire_at=?, claimed_at=NULL, updated_at=?
                       WHERE id=?""",
                    (self.next_schedule_at(schedule, now), now, reminder_id),
                )
            self._db.execute(
                """INSERT INTO messages(role, content, created_at, source_event_ids_json)
                   VALUES ('assistant', ?, ?, ?)""",
                (row["text"], now, json.dumps([f"reminder:{reminder_id}"])),
            )
            self._db.execute(
                """INSERT OR IGNORE INTO outbox(turn_id, dedupe_key, text)
                   VALUES (?, ?, ?)""",
                (
                    f"reminder:{reminder_id}:{occurrence}",
                    f"reminder:{reminder_id}:{occurrence}",
                    row["text"],
                ),
            )
        return True

    @staticmethod
    def _goal_dict(row: sqlite3.Row) -> dict[str, object]:
        goal = dict(row)
        goal["plan"] = json.loads(str(goal.pop("plan_json")))
        schedule_json = str(goal.pop("schedule_json", ""))
        goal["schedule"] = json.loads(schedule_json) if schedule_json else None
        return goal

    @staticmethod
    def normalize_schedule(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ValueError("schedule must be an object")
        kind = str(value.get("kind") or "")
        timezone = str(value.get("timezone") or "")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("schedule.timezone must be a valid IANA timezone") from None
        if kind == "interval":
            every_seconds = int(value.get("every_seconds", 0))
            if every_seconds < 60:
                raise ValueError("interval schedule requires every_seconds >= 60")
            return {
                "kind": kind,
                "timezone": timezone,
                "every_seconds": every_seconds,
            }
        if kind == "daily":
            at = str(value.get("at") or "")
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
                raise ValueError("daily schedule requires at in HH:MM format")
            return {"kind": kind, "timezone": timezone, "at": at}
        raise ValueError("schedule.kind must be interval or daily")

    @staticmethod
    def next_schedule_at(
        schedule: dict[str, object], after: float | None = None
    ) -> float:
        normalized = Store.normalize_schedule(schedule)
        after = time.time() if after is None else after
        if normalized["kind"] == "interval":
            return after + int(normalized["every_seconds"])
        zone = ZoneInfo(str(normalized["timezone"]))
        hour, minute = (int(part) for part in str(normalized["at"]).split(":"))
        local = datetime.fromtimestamp(after, zone)
        candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate.timestamp() <= after:
            candidate += timedelta(days=1)
        return candidate.timestamp()

    def claim_due_goal(self) -> dict[str, object] | None:
        now = time.time()
        with self._db:
            row = self._db.execute(
                """SELECT * FROM goals
                   WHERE status IN ('active', 'waiting')
                     AND COALESCE(retry_at, next_review_at) <= ?
                     AND review_claimed_at IS NULL
                   ORDER BY COALESCE(retry_at, next_review_at) LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                "UPDATE goals SET review_claimed_at=? WHERE id=?",
                (now, row["id"]),
            )
        return self._goal_dict(row)

    def next_goal_due_at(self) -> float | None:
        row = self._db.execute(
            """SELECT MIN(COALESCE(retry_at, next_review_at)) AS due FROM goals
               WHERE status IN ('active', 'waiting') AND review_claimed_at IS NULL"""
        ).fetchone()
        return float(row["due"]) if row and row["due"] is not None else None

    def release_goal_claim(self, goal_id: str, *, defer_seconds: float = 0) -> None:
        with self._db:
            if defer_seconds:
                self._db.execute(
                    """UPDATE goals SET review_claimed_at=NULL, next_review_at=?,
                       retry_at=NULL, failure_count=0, updated_at=?
                       WHERE id=? AND status IN ('active', 'waiting')""",
                    (time.time() + defer_seconds, time.time(), goal_id),
                )
            else:
                self._db.execute(
                    "UPDATE goals SET review_claimed_at=NULL WHERE id=?", (goal_id,)
                )

    def defer_goal_failure(self, goal_id: str) -> float | None:
        row = self._db.execute(
            "SELECT failure_count FROM goals WHERE id=?", (goal_id,)
        ).fetchone()
        if row is None:
            return None
        count = int(row["failure_count"]) + 1
        delays = (300, 900, 3600, 10800, 21600)
        retry_at = time.time() + delays[min(count - 1, len(delays) - 1)]
        with self._db:
            self._db.execute(
                """UPDATE goals SET failure_count=?, retry_at=?,
                   review_claimed_at=NULL, updated_at=? WHERE id=?""",
                (count, retry_at, time.time(), goal_id),
            )
        return retry_at

    def begin_tool_call(
        self,
        turn_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        capability: str = "external_effect",
    ) -> dict[str, object] | None:
        if capability not in {"read", "write", "external_effect"}:
            raise ValueError("invalid tool capability")
        serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        row = self._db.execute(
            """SELECT tool_name, arguments_sha256, state, result_json
               FROM tool_audit WHERE turn_id=? AND tool_call_id=?""",
            (turn_id, tool_call_id),
        ).fetchone()
        if row is not None:
            if row["tool_name"] != tool_name or row["arguments_sha256"] != digest:
                return {"ok": False, "error": "tool_call_id_conflict"}
            if row["state"] == "completed" and row["result_json"] is not None:
                return json.loads(str(row["result_json"]))
            return {
                "ok": False,
                "error": "previous_call_incomplete",
                "ambiguous": True,
            }
        with self._db:
            if capability != "read":
                self._db.execute(
                    """UPDATE turns SET external_effect_started=1,
                       stage='tool_dispatch', updated_at=?
                       WHERE id=? AND state='running'""",
                    (time.time(), turn_id),
                )
            else:
                self._db.execute(
                    """UPDATE turns SET stage='tool_dispatch', updated_at=?
                       WHERE id=? AND state='running'""",
                    (time.time(), turn_id),
                )
            self._db.execute(
                """INSERT INTO tool_audit
                   (turn_id, tool_call_id, tool_name, capability, arguments_sha256,
                    state, started_at)
                   VALUES (?, ?, ?, ?, ?, 'dispatching', ?)""",
                (turn_id, tool_call_id, tool_name, capability, digest, time.time()),
            )
        return None

    def complete_tool_call(
        self, turn_id: str, tool_call_id: str, result: dict[str, object]
    ) -> None:
        with self._db:
            self._db.execute(
                """UPDATE tool_audit
                   SET state='completed', result_json=?, ok=?, completed_at=?
                   WHERE turn_id=? AND tool_call_id=?""",
                (
                    json.dumps(result, ensure_ascii=False),
                    int(bool(result.get("ok"))),
                    time.time(),
                    turn_id,
                    tool_call_id,
                ),
            )
            self._db.execute(
                """UPDATE turns SET stage='tool_completed', updated_at=?
                   WHERE id=? AND state='running'""",
                (time.time(), turn_id),
            )

    def memory_context(
        self, query: str, max_results: int, token_budget: int
    ) -> str:
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

    def queue_progress(
        self, turn_id: str, tool_call_id: str, messages: list[ChannelMessage]
    ) -> None:
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE turns SET external_effect_started=1,
                   stage='message_dispatch', updated_at=?
                   WHERE id=? AND state='running'""",
                (now, turn_id),
            )
            for index, message in enumerate(messages):
                text, kind, path, payload = self._outbox_content(message)
                self._db.execute(
                    """INSERT OR IGNORE INTO turn_progress
                       (turn_id, tool_call_id, part_index, text, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (turn_id, tool_call_id, index, text, now),
                )
                self._db.execute(
                    """INSERT OR IGNORE INTO outbox
                       (turn_id, dedupe_key, text, kind, media_path, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        turn_id,
                        f"turn:{turn_id}:progress:{tool_call_id}:{index}",
                        text,
                        kind,
                        path,
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    ),
                )

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
            lexical_score = overlap / max(1, math.sqrt(len(query_units) * len(memory_units)))
            score = lexical_score + float(row["importance"]) * 0.1 + (1.0 if core else 0.0)
            ranked.append((score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [dict(row) for _, row in ranked[:max_results]]

    def has_memory(self, kind: str, key: str) -> bool:
        return self._db.execute(
            """SELECT 1 FROM memories AS m
               WHERE m.kind=? AND m.key=? AND m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )""",
            (kind, key, time.time()),
        ).fetchone() is not None

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

    def commit_turn(
        self,
        events: list[IncomingMessage],
        user_text: str,
        reply: AgentReply,
        draft: TurnDraft | None = None,
        turn_id: str | None = None,
    ) -> str:
        assistant_messages = reply.messages
        if not assistant_messages:
            raise ValueError("assistant messages must not be empty")
        normalized_messages = [self._outbox_content(message) for message in assistant_messages]
        turn_id = turn_id or uuid.uuid4().hex
        event_ids = [event.event_id for event in events]
        now = time.time()
        with self._db:
            self._db.execute(
                "INSERT INTO messages(role, content, created_at, source_event_ids_json) VALUES ('user', ?, ?, ?)",
                (user_text, now, json.dumps(event_ids, ensure_ascii=False)),
            )
            progress = self._db.execute(
                """SELECT text, created_at FROM turn_progress
                   WHERE turn_id=? ORDER BY created_at, tool_call_id, part_index""",
                (turn_id,),
            ).fetchall()
            self._db.executemany(
                """INSERT INTO messages(role, content, created_at, source_event_ids_json)
                   VALUES ('assistant', ?, ?, ?)""",
                (
                    (row["text"], row["created_at"], json.dumps(event_ids, ensure_ascii=False))
                    for row in progress
                ),
            )
            for index, (assistant_text, kind, path, payload) in enumerate(
                normalized_messages
            ):
                self._db.execute(
                    "INSERT INTO messages(role, content, created_at, source_event_ids_json) VALUES ('assistant', ?, ?, ?)",
                    (assistant_text, now, json.dumps(event_ids, ensure_ascii=False)),
                )
                self._db.execute(
                    """INSERT INTO outbox
                       (turn_id, dedupe_key, text, kind, media_path, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        turn_id,
                        f"turn:{turn_id}:{index}",
                        assistant_text,
                        kind,
                        path,
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
            if reply.continuity is not None:
                continuity = self._serialize_continuity(
                    reply.continuity, event_ids, now
                )
                self._db.execute(
                    """INSERT INTO continuity_state
                       (id, content, source_event_ids_json, updated_at)
                       VALUES (1, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                         content=excluded.content,
                         source_event_ids_json=excluded.source_event_ids_json,
                         updated_at=excluded.updated_at""",
                    (
                        continuity,
                        json.dumps(event_ids, ensure_ascii=False),
                        now,
                    ),
                )
            self._apply_mood_transition(reply.mood_transition, now)
            for memory in draft.memories if draft else []:
                self._remember(memory, events, now)
            for conflict in draft.memory_conflicts if draft else []:
                self._propose_memory_conflict(conflict, events, now)
            for forgotten in draft.forgotten_memories if draft else []:
                self._forget_memory(forgotten, events, now)
            self._apply_goal_mutations(draft, now)
            self._apply_reminder_mutations(draft, now)
            self._db.executemany(
                "UPDATE events SET processed=1 WHERE id=?", ((event_id,) for event_id in event_ids)
            )
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   failure_reason=NULL, updated_at=? WHERE id=?""",
                (now, turn_id),
            )
        return turn_id

    def commit_autonomous_turn(
        self, goal_id: str, draft: TurnDraft, turn_id: str | None = None
    ) -> str:
        turn_id = turn_id or uuid.uuid4().hex
        now = time.time()
        with self._db:
            self._apply_goal_mutations(draft, now)
            self._apply_reminder_mutations(draft, now)
            if goal_id not in draft.goals:
                current = self.goal(goal_id)
                next_review_at = (
                    self.next_schedule_at(current["schedule"], now)
                    if current and current.get("schedule")
                    else now + 900
                )
                self._db.execute(
                    """UPDATE goals SET review_claimed_at=NULL, next_review_at=?,
                       retry_at=NULL, failure_count=0, updated_at=?
                       WHERE id=? AND status IN ('active', 'waiting')""",
                    (next_review_at, now, goal_id),
                )
            if draft.notification_messages:
                self._db.execute(
                    """INSERT OR IGNORE INTO notifications
                       (id, turn_id, goal_id, notification_key, priority, reason,
                        messages_json, state, not_before, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    (
                        f"notification:{turn_id}",
                        turn_id,
                        goal_id,
                        draft.notification_key or f"goal.{goal_id}",
                        draft.notification_priority,
                        draft.notification_reason,
                        json.dumps(draft.notification_messages, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   failure_reason=NULL, updated_at=? WHERE id=?""",
                (now, turn_id),
            )
        return turn_id

    @staticmethod
    def _local_day_bounds(now: float, timezone: str) -> tuple[float, float]:
        zone = ZoneInfo(timezone)
        local = datetime.fromtimestamp(now, zone)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp(), (start + timedelta(days=1)).timestamp()

    @staticmethod
    def _quiet_until(now: float, config: NotificationConfig) -> float:
        if not config.quiet_start or not config.quiet_end:
            return now
        zone = ZoneInfo(config.timezone)
        local = datetime.fromtimestamp(now, zone)
        start_hour, start_minute = map(int, config.quiet_start.split(":"))
        end_hour, end_minute = map(int, config.quiet_end.split(":"))
        minute = local.hour * 60 + local.minute
        start = start_hour * 60 + start_minute
        end = end_hour * 60 + end_minute
        in_quiet = start <= minute < end if start < end else minute >= start or minute < end
        if not in_quiet:
            return now
        end_local = local.replace(
            hour=end_hour, minute=end_minute, second=0, microsecond=0
        )
        if start > end and minute >= start:
            end_local += timedelta(days=1)
        return end_local.timestamp()

    def _notification_not_before(
        self, row: sqlite3.Row, config: NotificationConfig, now: float
    ) -> float:
        priority = str(row["priority"])
        eligible = now
        if priority == "normal":
            eligible = max(eligible, self._quiet_until(now, config))
            last = self._db.execute(
                """SELECT MAX(queued_at) FROM notifications
                   WHERE state='queued' AND notification_key=?""",
                (row["notification_key"],),
            ).fetchone()[0]
            if last is not None:
                eligible = max(eligible, float(last) + config.cooldown_seconds)
            if self._db.execute(
                "SELECT 1 FROM events WHERE processed=0 LIMIT 1"
            ).fetchone():
                eligible = max(eligible, now + config.pending_owner_delay_seconds)
        day_start, next_day = self._local_day_bounds(now, config.timezone)
        budget = (
            config.urgent_daily_budget if priority == "urgent" else config.daily_budget
        )
        used = self._db.execute(
            """SELECT COUNT(*) FROM notifications
               WHERE state='queued' AND priority=? AND queued_at>=? AND queued_at<?""",
            (priority, day_start, next_day),
        ).fetchone()[0]
        if used >= budget:
            eligible = max(eligible, next_day)
        return eligible

    def claim_due_notification(
        self, config: NotificationConfig, now: float | None = None
    ) -> dict[str, object] | None:
        now = time.time() if now is None else now
        with self._db:
            row = self._db.execute(
                """SELECT * FROM notifications
                   WHERE state='pending' AND claimed_at IS NULL AND not_before<=?
                   ORDER BY not_before, created_at LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            eligible = self._notification_not_before(row, config, now)
            if eligible > now:
                self._db.execute(
                    "UPDATE notifications SET not_before=? WHERE id=?",
                    (eligible, row["id"]),
                )
                return None
            self._db.execute(
                "UPDATE notifications SET claimed_at=? WHERE id=?", (now, row["id"])
            )
        return dict(row)

    def next_notification_due_at(self) -> float | None:
        row = self._db.execute(
            """SELECT MIN(not_before) FROM notifications
               WHERE state='pending' AND claimed_at IS NULL"""
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def queue_notification(
        self,
        notification_id: str,
        now: float | None = None,
        config: NotificationConfig | None = None,
    ) -> bool:
        now = time.time() if now is None else now
        with self._db:
            row = self._db.execute(
                """SELECT * FROM notifications
                   WHERE id=? AND state='pending' AND claimed_at IS NOT NULL""",
                (notification_id,),
            ).fetchone()
            if row is None:
                return False
            if config is not None:
                eligible = self._notification_not_before(row, config, now)
                if eligible > now:
                    self._db.execute(
                        """UPDATE notifications SET claimed_at=NULL, not_before=?
                           WHERE id=?""",
                        (eligible, notification_id),
                    )
                    return False
            messages = json.loads(str(row["messages_json"]))
            source = (
                f"heartbeat:{row['turn_id']}"
                if row["goal_id"] == "heartbeat"
                else f"goal:{row['goal_id']}"
            )
            for index, message in enumerate(messages):
                visible, kind, path, payload = self._outbox_content(message)
                self._db.execute(
                    """INSERT INTO messages(role, content, created_at, source_event_ids_json)
                       VALUES ('assistant', ?, ?, ?)""",
                    (visible, now, json.dumps([source])),
                )
                self._db.execute(
                    """INSERT OR IGNORE INTO outbox
                       (turn_id, dedupe_key, text, kind, media_path, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        row["turn_id"],
                        f"notification:{notification_id}:{index}",
                        visible,
                        kind,
                        path,
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
            self._db.execute(
                """UPDATE notifications SET state='queued', claimed_at=NULL, queued_at=?
                   WHERE id=?""",
                (now, notification_id),
            )
        return True

    def _apply_goal_mutations(self, draft: TurnDraft | None, now: float) -> None:
        for goal in draft.goals.values() if draft else []:
            self._db.execute(
                """INSERT INTO goals
                   (id, title, success_criteria, authority, source_event_id, status,
                    plan_json, next_action, waiting_for, blocked_reason, latest_result, schedule_json,
                    next_review_at, review_claimed_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     title=excluded.title,
                     success_criteria=excluded.success_criteria,
                     status=excluded.status,
                     plan_json=excluded.plan_json,
                     next_action=excluded.next_action,
                     waiting_for=excluded.waiting_for,
                     blocked_reason=excluded.blocked_reason,
                     latest_result=excluded.latest_result,
                     schedule_json=excluded.schedule_json,
                     next_review_at=excluded.next_review_at,
                     retry_at=NULL,
                     failure_count=0,
                     review_claimed_at=NULL,
                     updated_at=excluded.updated_at""",
                (
                    goal["id"], goal["title"], goal["success_criteria"], goal["authority"],
                    goal["source_event_id"], goal["status"],
                    json.dumps(goal.get("plan", []), ensure_ascii=False),
                    goal.get("next_action", ""), goal.get("waiting_for", ""),
                    goal.get("blocked_reason", ""), goal.get("latest_result", ""),
                    json.dumps(goal.get("schedule"), ensure_ascii=False) if goal.get("schedule") else "",
                    goal.get("next_review_at"), now, now,
                ),
            )

    def _apply_reminder_mutations(self, draft: TurnDraft | None, now: float) -> None:
        for reminder in draft.reminders.values() if draft else []:
            self._db.execute(
                """INSERT INTO reminders
                   (id, text, source_event_id, status, fire_at, schedule_json,
                    claimed_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     text=excluded.text,
                     status=excluded.status,
                     fire_at=excluded.fire_at,
                     schedule_json=excluded.schedule_json,
                     claimed_at=NULL,
                     updated_at=excluded.updated_at""",
                (
                    reminder["id"],
                    reminder["text"],
                    reminder["source_event_id"],
                    reminder["status"],
                    reminder["fire_at"],
                    json.dumps(reminder.get("schedule"), ensure_ascii=False)
                    if reminder.get("schedule")
                    else "",
                    now,
                    now,
                ),
            )

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

    def create_webhook_run(
        self,
        workflow_id: str,
        idempotency_key: str | None,
        plan: dict[str, object],
    ) -> tuple[dict[str, object], bool]:
        if idempotency_key is not None:
            existing = self._db.execute(
                """SELECT id, workflow_id, state FROM webhook_runs
                   WHERE workflow_id=? AND idempotency_key=?""",
                (workflow_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return dict(existing), False
        run_id = uuid.uuid4().hex
        now = time.time()
        steps = plan.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("webhook plan needs steps")
        try:
            with self._db:
                self._db.execute(
                    """INSERT INTO webhook_runs
                       (id, workflow_id, idempotency_key, plan_json, state,
                        current_step, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'queued', 0, ?, ?)""",
                    (
                        run_id,
                        workflow_id,
                        idempotency_key,
                        json.dumps(plan, ensure_ascii=False, separators=(",", ":")),
                        now,
                        now,
                    ),
                )
                self._db.executemany(
                    """INSERT INTO webhook_steps
                       (run_id, step_index, step_id, kind, state)
                       VALUES (?, ?, ?, ?, 'queued')""",
                    (
                        (run_id, index, str(step["id"]), str(step["uses"]))
                        for index, step in enumerate(steps)
                    ),
                )
        except sqlite3.IntegrityError:
            if idempotency_key is None:
                raise
            existing = self._db.execute(
                """SELECT id, workflow_id, state FROM webhook_runs
                   WHERE workflow_id=? AND idempotency_key=?""",
                (workflow_id, idempotency_key),
            ).fetchone()
            if existing is None:
                raise
            return dict(existing), False
        return {"id": run_id, "workflow_id": workflow_id, "state": "queued"}, True

    def webhook_run(self, run_id: str) -> dict[str, object] | None:
        row = self._db.execute(
            """SELECT id, workflow_id, state, current_step, error,
                      created_at, updated_at
               FROM webhook_runs WHERE id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        steps = self._db.execute(
            """SELECT step_index, step_id, kind, state, error, started_at, completed_at
               FROM webhook_steps WHERE run_id=? ORDER BY step_index""",
            (run_id,),
        ).fetchall()
        return {
            "run_id": row["id"],
            "workflow": row["workflow_id"],
            "state": row["state"],
            "current_step": row["current_step"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "steps": [dict(step) for step in steps],
        }

    def claim_webhook_run(self) -> dict[str, object] | None:
        with self._db:
            row = self._db.execute(
                """SELECT id, workflow_id, plan_json, state, current_step
                   FROM webhook_runs
                   WHERE state IN ('queued', 'waiting_delivery')
                   ORDER BY created_at LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            if row["state"] == "queued":
                self._db.execute(
                    "UPDATE webhook_runs SET state='running', updated_at=? WHERE id=?",
                    (time.time(), row["id"]),
                )
        result = dict(row)
        result["state"] = "running" if row["state"] == "queued" else row["state"]
        result["plan"] = json.loads(str(row["plan_json"]))
        return result

    def webhook_step(self, run_id: str, step_index: int) -> dict[str, object] | None:
        row = self._db.execute(
            """SELECT run_id, step_index, step_id, kind, state, result_json, error
               FROM webhook_steps WHERE run_id=? AND step_index=?""",
            (run_id, step_index),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["result"] = json.loads(str(row["result_json"]))
        except json.JSONDecodeError:
            result["result"] = {}
        return result

    def start_webhook_step(self, run_id: str, step_index: int) -> None:
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE webhook_steps SET state='running', started_at=?,
                   completed_at=NULL, error=NULL
                   WHERE run_id=? AND step_index=? AND state='queued'""",
                (now, run_id, step_index),
            )
            self._db.execute(
                """UPDATE webhook_runs SET state='running', current_step=?,
                   error=NULL, updated_at=? WHERE id=?""",
                (step_index, now, run_id),
            )

    def queue_webhook_messages(
        self, run_id: str, step_index: int, messages: list[ChannelMessage]
    ) -> list[int]:
        step = self.webhook_step(run_id, step_index)
        if step is None:
            raise ValueError("webhook step not found")
        if step["state"] in {"waiting_delivery", "succeeded"}:
            result = step["result"]
            return [int(value) for value in result.get("outbox_ids", [])]  # type: ignore[union-attr]
        if step["state"] != "running":
            raise ValueError("webhook message step is not running")
        if not messages:
            raise ValueError("webhook messages must not be empty")
        source = json.dumps([f"webhook:{run_id}:{step_index}"], ensure_ascii=False)
        now = time.time()
        outbox_ids: list[int] = []
        with self._db:
            for index, message in enumerate(messages):
                text, kind, path, payload = self._outbox_content(message)
                self._db.execute(
                    """INSERT INTO messages(role, content, created_at, source_event_ids_json)
                       VALUES ('assistant', ?, ?, ?)""",
                    (text, now, source),
                )
                cursor = self._db.execute(
                    """INSERT INTO outbox
                       (turn_id, dedupe_key, text, kind, media_path, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        f"webhook:{run_id}",
                        f"webhook:{run_id}:{step_index}:{index}",
                        text,
                        kind,
                        path,
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                outbox_ids.append(int(cursor.lastrowid))
            result_json = json.dumps({"outbox_ids": outbox_ids}, separators=(",", ":"))
            self._db.execute(
                """UPDATE webhook_steps SET state='waiting_delivery', result_json=?
                   WHERE run_id=? AND step_index=?""",
                (result_json, run_id, step_index),
            )
            self._db.execute(
                """UPDATE webhook_runs SET state='waiting_delivery', updated_at=?
                   WHERE id=?""",
                (now, run_id),
            )
        return outbox_ids

    def webhook_delivery_state(self, run_id: str, step_index: int) -> str:
        step = self.webhook_step(run_id, step_index)
        if step is None:
            return "failed"
        result = step["result"]
        ids = [int(value) for value in result.get("outbox_ids", [])]  # type: ignore[union-attr]
        if not ids:
            return "failed"
        placeholders = ",".join("?" for _ in ids)
        rows = self._db.execute(
            f"SELECT state FROM outbox WHERE id IN ({placeholders})", ids
        ).fetchall()
        states = {str(row["state"]) for row in rows}
        if len(rows) != len(ids) or "failed" in states:
            return "failed"
        return "succeeded" if states == {"sent"} else "pending"

    def finish_webhook_step(
        self,
        run_id: str,
        step_index: int,
        state: str,
        result: dict[str, object],
        error: str | None,
    ) -> None:
        if state not in {"succeeded", "failed", "ambiguous"}:
            raise ValueError("invalid webhook terminal state")
        now = time.time()
        with self._db:
            if result:
                result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                self._db.execute(
                    """UPDATE webhook_steps SET state=?, result_json=?, error=?, completed_at=?
                       WHERE run_id=? AND step_index=?""",
                    (state, result_json, error, now, run_id, step_index),
                )
            else:
                self._db.execute(
                    """UPDATE webhook_steps SET state=?, error=?, completed_at=?
                       WHERE run_id=? AND step_index=?""",
                    (state, error, now, run_id, step_index),
                )
            run_state = "running" if state == "succeeded" else state
            self._db.execute(
                """UPDATE webhook_runs SET state=?, current_step=?, error=?, updated_at=?
                   WHERE id=?""",
                (
                    run_state,
                    step_index + 1 if state == "succeeded" else step_index,
                    error,
                    now,
                    run_id,
                ),
            )

    def complete_webhook_run(self, run_id: str) -> None:
        with self._db:
            self._db.execute(
                """UPDATE webhook_runs SET state='succeeded', error=NULL, updated_at=?
                   WHERE id=? AND NOT EXISTS (
                       SELECT 1 FROM webhook_steps
                       WHERE run_id=? AND state!='succeeded'
                   )""",
                (time.time(), run_id, run_id),
            )

    def fail_webhook_run(self, run_id: str, error: str) -> None:
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE webhook_runs SET state='failed', error=?, updated_at=?
                   WHERE id=? AND state NOT IN ('succeeded', 'ambiguous')""",
                (error[:500], now, run_id),
            )
            self._db.execute(
                """UPDATE webhook_steps SET state='failed', error=?, completed_at=?
                   WHERE run_id=? AND state='running'""",
                (error[:500], now, run_id),
            )

    def due_outbox(self) -> list[OutboxMessage]:
        rows = self._db.execute(
            """SELECT o.id, o.turn_id, o.text, o.state, o.attempts,
                      o.kind, o.media_path, o.payload_json
               FROM outbox AS o
               WHERE o.state IN ('pending', 'ambiguous')
                 AND o.next_attempt_at <= ?
                 AND NOT EXISTS (
                     SELECT 1 FROM outbox AS earlier
                     WHERE earlier.id < o.id
                       AND earlier.state NOT IN ('sent', 'failed')
                 )
               ORDER BY o.id LIMIT 1""",
            (time.time(),),
        ).fetchall()
        messages: list[OutboxMessage] = []
        for row in rows:
            raw = str(row["payload_json"] or "")
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                payload = None
            if not isinstance(payload, dict):
                if row["kind"] == "image" and row["media_path"]:
                    payload = {
                        "action": "message",
                        "segments": [
                            {"type": "image", "data": {"file": row["media_path"]}}
                        ],
                    }
                else:
                    payload = normalize_channel_message(str(row["text"]))
            stored_media = str(row["media_path"] or "")
            resolved_media = (
                str(self._resolve_asset_path(stored_media)) if stored_media else None
            )
            if isinstance(payload, dict) and stored_media and resolved_media:
                for segment in payload.get("segments") or []:
                    data = segment.get("data") if isinstance(segment, dict) else None
                    if isinstance(data, dict) and data.get("file") == stored_media:
                        data["file"] = resolved_media
            messages.append(
                OutboxMessage(
                    id=row["id"],
                    turn_id=row["turn_id"],
                    text=row["text"],
                    state=row["state"],
                    attempts=row["attempts"],
                    kind=row["kind"],
                    media_path=resolved_media,
                    payload=payload,
                )
            )
        return messages

    def mark_sending(self, outbox_id: int) -> None:
        with self._db:
            self._db.execute(
                "UPDATE outbox SET state='sending', attempts=attempts+1 WHERE id=?",
                (outbox_id,),
            )

    def mark_not_dispatched(self, outbox_id: int, error: str) -> None:
        with self._db:
            self._db.execute(
                """UPDATE outbox SET state='pending', attempts=MAX(0, attempts-1),
                   next_attempt_at=?, last_error=? WHERE id=?""",
                (time.time() + 2, error, outbox_id),
            )

    def mark_sent(self, outbox_id: int) -> None:
        with self._db:
            self._db.execute(
                "UPDATE outbox SET state='sent', last_error=NULL WHERE id=?", (outbox_id,)
            )

    def mark_ambiguous(self, outbox_id: int, attempts: int, error: str) -> None:
        state = "ambiguous" if attempts < 2 else "failed"
        with self._db:
            self._db.execute(
                """UPDATE outbox SET state=?, possible_duplicate=1,
                   next_attempt_at=?, last_error=? WHERE id=?""",
                (state, time.time() + 2, error, outbox_id),
            )

    def mark_failed(self, outbox_id: int, error: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE outbox SET state='failed', last_error=? WHERE id=?",
                (error, outbox_id),
            )
