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
from zoneinfo import ZoneInfo

from ..channel import (
    ChannelMessage,
    media_path,
    normalize_channel_message,
    render_channel_message,
)
from ..config import HeartbeatConfig, NotificationConfig, ReflectionConfig
from ..emotions import emotion_slug, valid_emotion_slug
from ..models import (
    AgentReply,
    IncomingMessage,
    TurnDraft,
)
from .delivery import DeliveryStore
from .memory import MemoryStore, estimate_tokens, lexical_units
from .scheduling import next_schedule_at, quiet_until


logger = logging.getLogger(__name__)


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
REFLECTION_MEMORY_KINDS = {
    "owner_profile",
    "owner_preference",
    "world_knowledge",
    "self_insight",
    "relationship",
    "shared_experience",
    "practice",
}


class Store(MemoryStore, DeliveryStore):
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
        self._db.executescript(Path(__file__).with_name("schema.sql").read_text())
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
        if "reply_expectation" not in outbox_columns:
            self._db.execute(
                """ALTER TABLE outbox ADD COLUMN reply_expectation
                   TEXT NOT NULL DEFAULT ''"""
            )
        if "target_channel" not in outbox_columns:
            self._db.execute(
                "ALTER TABLE outbox ADD COLUMN target_channel TEXT NOT NULL DEFAULT ''"
            )
        event_columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(events)").fetchall()
        }
        if "payload_json" not in event_columns:
            self._db.execute(
                "ALTER TABLE events ADD COLUMN payload_json TEXT NOT NULL DEFAULT ''"
            )
        message_columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "turn_id" not in message_columns:
            self._db.execute(
                "ALTER TABLE messages ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''"
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
        self_state_columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(self_state)").fetchall()
        }
        if "activity_result" not in self_state_columns:
            self._db.execute(
                "ALTER TABLE self_state ADD COLUMN activity_result TEXT NOT NULL DEFAULT ''"
            )
        for name, definition in (
            ("pending_reply_turn_id", "TEXT"),
            ("pending_reply_expectation", "TEXT NOT NULL DEFAULT ''"),
            ("pending_reply_since", "REAL"),
            ("pending_reply_checks", "INTEGER NOT NULL DEFAULT 0"),
            ("pending_reply_channel", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in self_state_columns:
                self._db.execute(
                    f"ALTER TABLE self_state ADD COLUMN {name} {definition}"
                )
        reminder_columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(reminders)").fetchall()
        }
        if "schedule_json" not in reminder_columns:
            self._db.execute(
                "ALTER TABLE reminders ADD COLUMN schedule_json TEXT NOT NULL DEFAULT ''"
            )
        notification_columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(notifications)").fetchall()
        }
        if "reply_expectation" not in notification_columns:
            self._db.execute(
                """ALTER TABLE notifications ADD COLUMN reply_expectation
                   TEXT NOT NULL DEFAULT ''"""
            )
        if "target_channel" not in notification_columns:
            self._db.execute(
                """ALTER TABLE notifications ADD COLUMN target_channel
                   TEXT NOT NULL DEFAULT ''"""
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
            "UPDATE self_state SET next_heartbeat_at=0 WHERE next_heartbeat_at IS NULL"
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
        self._db.execute(
            "UPDATE notifications SET claimed_at=NULL WHERE state='pending'"
        )
        self._db.execute("UPDATE self_state SET heartbeat_claimed_at=NULL WHERE id=1")
        self._db.execute(
            "UPDATE reflections SET state='pending', claimed_at=NULL WHERE state='running'"
        )
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

    def assign_legacy_outbox_channel(self, primary_channel: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE outbox SET target_channel=? WHERE target_channel=''",
                (primary_channel,),
            )

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
        with self._db:
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
            if cursor.rowcount == 1:
                self._db.execute(
                    """UPDATE self_state SET pending_reply_turn_id=NULL,
                       pending_reply_expectation='', pending_reply_since=NULL,
                       pending_reply_checks=0, pending_reply_channel='',
                       updated_at=? WHERE id=1""",
                    (time.time(),),
                )
                self._db.execute(
                    """DELETE FROM notifications
                       WHERE notification_key='heartbeat.reply_followup'
                         AND state='pending'"""
                )
                self._db.execute(
                    """UPDATE outbox SET state='failed',
                       last_error='owner_replied_before_followup'
                       WHERE state='pending' AND turn_id IN (
                           SELECT turn_id FROM notifications
                           WHERE notification_key='heartbeat.reply_followup'
                       )"""
                )
        return cursor.rowcount == 1

    def pending_events(self) -> list[IncomingMessage]:
        rows = self._db.execute(
            "SELECT * FROM events WHERE processed=0 ORDER BY received_at, rowid"
        ).fetchall()
        return [self._incoming_message(row) for row in rows]

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
            if row["state"] == "running":
                self._db.execute(
                    """UPDATE turns SET stage='started', failure_reason=NULL,
                       llm_calls=0, input_tokens=0, output_tokens=0,
                       started_at=?, updated_at=? WHERE id=?""",
                    (now, now, turn_id),
                )
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

    @staticmethod
    def _context_plan_dict(row: sqlite3.Row) -> dict[str, object]:
        plan = dict(row)
        plan["source_event_ids"] = json.loads(
            str(plan.pop("source_event_ids_json"))
        )
        plan["plan"] = json.loads(str(plan.pop("plan_json")))
        plan["retrieval"] = json.loads(str(plan.pop("retrieval_json")))
        return plan

    def save_context_plan(
        self,
        turn_id: str,
        revision: int,
        source_event_ids: list[str],
        plan: dict[str, object],
        *,
        state: str = "planned",
    ) -> dict[str, object]:
        if revision < 1:
            raise ValueError("context plan revision must be positive")
        if state not in {"planned", "degraded"}:
            raise ValueError("a new context plan must be planned or degraded")
        source_json = json.dumps(
            source_event_ids, ensure_ascii=False, separators=(",", ":")
        )
        plan_json = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
        now = time.time()
        with self._db:
            existing = self._db.execute(
                "SELECT * FROM context_plans WHERE turn_id=? AND revision=?",
                (turn_id, revision),
            ).fetchone()
            if existing is not None:
                if (
                    json.loads(str(existing["source_event_ids_json"]))
                    != source_event_ids
                    or json.loads(str(existing["plan_json"])) != plan
                ):
                    raise ValueError("context plan revision already exists")
                return self._context_plan_dict(existing)
            latest = self._db.execute(
                "SELECT MAX(revision) FROM context_plans WHERE turn_id=?", (turn_id,)
            ).fetchone()[0]
            if latest is not None and int(latest) >= revision:
                raise ValueError("context plan revision must increase")
            self._db.execute(
                """UPDATE context_plans SET state='superseded', updated_at=?
                   WHERE turn_id=? AND state<>'superseded'""",
                (now, turn_id),
            )
            self._db.execute(
                """INSERT INTO context_plans
                   (turn_id, revision, source_event_ids_json, plan_json,
                    state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (turn_id, revision, source_json, plan_json, state, now, now),
            )
        saved = self.context_plan(turn_id, revision)
        if saved is None:
            raise RuntimeError("context plan was not saved")
        return saved

    def context_plan(
        self, turn_id: str, revision: int | None = None
    ) -> dict[str, object] | None:
        if revision is None:
            row = self._db.execute(
                """SELECT * FROM context_plans
                   WHERE turn_id=? AND state<>'superseded'
                   ORDER BY revision DESC LIMIT 1""",
                (turn_id,),
            ).fetchone()
        else:
            row = self._db.execute(
                "SELECT * FROM context_plans WHERE turn_id=? AND revision=?",
                (turn_id, revision),
            ).fetchone()
        return self._context_plan_dict(row) if row else None

    def save_context_retrieval(
        self,
        turn_id: str,
        revision: int,
        retrieval: dict[str, object],
        *,
        state: str = "recalled",
    ) -> dict[str, object]:
        if state not in {"recalled", "degraded"}:
            raise ValueError("context retrieval must be recalled or degraded")
        now = time.time()
        with self._db:
            cursor = self._db.execute(
                """UPDATE context_plans SET retrieval_json=?, state=?, updated_at=?
                   WHERE turn_id=? AND revision=? AND state<>'superseded'""",
                (
                    json.dumps(
                        retrieval, ensure_ascii=False, separators=(",", ":")
                    ),
                    state,
                    now,
                    turn_id,
                    revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("active context plan not found")
        saved = self.context_plan(turn_id, revision)
        if saved is None:
            raise RuntimeError("context retrieval was not saved")
        return saved

    def supersede_context_plan(self, turn_id: str, revision: int) -> bool:
        with self._db:
            cursor = self._db.execute(
                """UPDATE context_plans SET state='superseded', updated_at=?
                   WHERE turn_id=? AND revision=? AND state<>'superseded'""",
                (time.time(), turn_id, revision),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _episode_dict(row: sqlite3.Row) -> dict[str, object]:
        episode = dict(row)
        for name in ("topics", "entities", "open_loops"):
            episode[name] = json.loads(str(episode.pop(f"{name}_json")))
        return episode

    def create_episode(
        self,
        title: str,
        *,
        episode_id: str | None = None,
        topics: list[object] | None = None,
        entities: list[object] | None = None,
        open_loops: list[object] | None = None,
        salience: float = 0.5,
    ) -> dict[str, object]:
        title = title.strip()
        if not title:
            raise ValueError("episode title is required")
        if not 0 <= salience <= 1:
            raise ValueError("episode salience must be between 0 and 1")
        episode_id = episode_id or uuid.uuid4().hex
        now = time.time()
        with self._db:
            self._db.execute(
                """INSERT INTO conversation_episodes
                   (id, title, topics_json, entities_json, open_loops_json,
                    salience, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    episode_id,
                    title[:200],
                    json.dumps(topics or [], ensure_ascii=False, separators=(",", ":")),
                    json.dumps(
                        entities or [], ensure_ascii=False, separators=(",", ":")
                    ),
                    json.dumps(
                        open_loops or [], ensure_ascii=False, separators=(",", ":")
                    ),
                    salience,
                    now,
                    now,
                ),
            )
        saved = self.episode(episode_id)
        if saved is None:
            raise RuntimeError("episode was not saved")
        return saved

    def episode(self, episode_id: str) -> dict[str, object] | None:
        row = self._db.execute(
            "SELECT * FROM conversation_episodes WHERE id=?", (episode_id,)
        ).fetchone()
        return self._episode_dict(row) if row else None

    def list_episode_candidates(self, limit: int = 20) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        rows = self._db.execute(
            """SELECT * FROM conversation_episodes
               WHERE status IN ('open', 'closing')
               ORDER BY status='open' DESC, salience DESC, updated_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [self._episode_dict(row) for row in rows]

    def link_turn_to_episode(
        self,
        episode_id: str,
        turn_id: str,
        *,
        relation: str = "primary",
        unit_ids: list[str] | None = None,
    ) -> dict[str, object]:
        if relation not in {"primary", "related"}:
            raise ValueError("episode turn relation must be primary or related")
        now = time.time()
        with self._db:
            row = self._db.execute(
                """SELECT ordinal FROM episode_turns
                   WHERE episode_id=? AND turn_id=?""",
                (episode_id, turn_id),
            ).fetchone()
            if row is None:
                ordinal = int(
                    self._db.execute(
                        """SELECT COALESCE(MAX(ordinal), 0) + 1 FROM episode_turns
                           WHERE episode_id=?""",
                        (episode_id,),
                    ).fetchone()[0]
                )
                self._db.execute(
                    """INSERT INTO episode_turns
                       (episode_id, turn_id, ordinal, relation, unit_ids_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        episode_id,
                        turn_id,
                        ordinal,
                        relation,
                        json.dumps(
                            unit_ids or [], ensure_ascii=False, separators=(",", ":")
                        ),
                    ),
                )
            else:
                ordinal = int(row["ordinal"])
                self._db.execute(
                    """UPDATE episode_turns SET relation=?, unit_ids_json=?
                       WHERE episode_id=? AND turn_id=?""",
                    (
                        relation,
                        json.dumps(
                            unit_ids or [], ensure_ascii=False, separators=(",", ":")
                        ),
                        episode_id,
                        turn_id,
                    ),
                )
            self._db.execute(
                "UPDATE conversation_episodes SET updated_at=? WHERE id=?",
                (now, episode_id),
            )
        return {
            "episode_id": episode_id,
            "turn_id": turn_id,
            "ordinal": ordinal,
            "relation": relation,
            "unit_ids": unit_ids or [],
        }

    def episode_turns(self, episode_id: str) -> list[dict[str, object]]:
        rows = self._db.execute(
            "SELECT * FROM episode_turns WHERE episode_id=? ORDER BY ordinal",
            (episode_id,),
        ).fetchall()
        return [
            {
                **{key: row[key] for key in row.keys() if key != "unit_ids_json"},
                "unit_ids": json.loads(str(row["unit_ids_json"])),
            }
            for row in rows
        ]

    def link_episodes(
        self, from_episode_id: str, to_episode_id: str, kind: str
    ) -> None:
        if kind not in {"continues", "references", "supersedes"}:
            raise ValueError("invalid episode link kind")
        with self._db:
            self._db.execute(
                """INSERT OR IGNORE INTO episode_links
                   (from_episode_id, to_episode_id, kind) VALUES (?, ?, ?)""",
                (from_episode_id, to_episode_id, kind),
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
            f"- turn_id={row['turn_id']} reason={row['reason']}" for row in rows
        )

    def resolve_reconciliation(
        self, turn_prefix: str, resolution: str, *, resume: bool
    ) -> dict[str, object]:
        prefix = turn_prefix.strip()
        resolution = resolution.strip()
        if len(prefix) < 8 or not re.fullmatch(r"[0-9a-f]+", prefix):
            raise ValueError(
                "turn id prefix must contain at least 8 hexadecimal characters"
            )
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
            if (
                selected
                and tokens + row_tokens > token_budget
                and user_turns >= min_turns
            ):
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
        row_tokens = [estimate_tokens(str(row["content"])) for row in rows]
        if sum(row_tokens) <= math.ceil(token_budget * 1.25):
            return None
        tokens = 0
        user_turns = 0
        keep_from = len(rows)
        for index in range(len(rows) - 1, -1, -1):
            row_token = row_tokens[index]
            if (
                keep_from < len(rows)
                and tokens + row_token > token_budget
                and user_turns >= min_turns
            ):
                break
            keep_from = index
            tokens += row_token
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
                item for value in state.get("open_loops", []) if (item := text(value))
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
            return (
                datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() > now
            )
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
                datetime.fromtimestamp(float(value))
                .astimezone()
                .isoformat(timespec="seconds")
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
                    "result": state["activity_result"],
                    "since": timestamp(state["activity_since"]),
                },
                "last_heartbeat_at": timestamp(state["last_heartbeat_at"]),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def pending_owner_reply(self, now: float | None = None) -> dict[str, object] | None:
        now = time.time() if now is None else now
        row = self._db.execute(
            """SELECT pending_reply_turn_id, pending_reply_expectation,
                      pending_reply_since, pending_reply_checks,
                      pending_reply_channel
               FROM self_state WHERE id=1"""
        ).fetchone()
        if row is None or not str(row["pending_reply_expectation"] or "").strip():
            return None
        since = float(row["pending_reply_since"] or now)
        followups = self._db.execute(
            """SELECT COUNT(*) FROM notifications AS notification
               WHERE notification.notification_key='heartbeat.reply_followup'
                 AND notification.created_at>=?
                 AND EXISTS (
                     SELECT 1 FROM outbox
                     WHERE outbox.turn_id=notification.turn_id
                       AND outbox.state='sent'
                 )""",
            (since,),
        ).fetchone()[0]
        return {
            "source_turn": str(row["pending_reply_turn_id"] or ""),
            "expected_response": str(row["pending_reply_expectation"]),
            "waiting_since": datetime.fromtimestamp(since)
            .astimezone()
            .isoformat(timespec="seconds"),
            "waiting_minutes": max(0, int((now - since) / 60)),
            "heartbeat_checks": int(row["pending_reply_checks"] or 0),
            "channel": str(row["pending_reply_channel"] or ""),
            "delivered_followups": int(followups or 0),
        }

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

    def ensure_heartbeat(
        self, config: HeartbeatConfig, now: float | None = None
    ) -> None:
        if not config.enabled:
            return
        now = time.time() if now is None else now
        with self._db:
            self._db.execute(
                """UPDATE self_state SET next_heartbeat_at=?, updated_at=?
                   WHERE id=1 AND next_heartbeat_at<=0""",
                (now + config.initial_delay_seconds, now),
            )

    def claim_due_heartbeat(
        self,
        config: HeartbeatConfig,
        notifications: NotificationConfig,
        now: float | None = None,
    ) -> dict[str, object] | None:
        now = time.time() if now is None else now
        with self._db:
            row = self._db.execute("SELECT * FROM self_state WHERE id=1").fetchone()
            if (
                row is None
                or (
                    not config.enabled
                    and not str(row["pending_reply_expectation"] or "").strip()
                )
                or row["heartbeat_claimed_at"] is not None
                or row["next_heartbeat_at"] is None
                or float(row["next_heartbeat_at"]) > now
            ):
                return None
            quiet_end = quiet_until(now, notifications)
            if quiet_end > now:
                self._db.execute(
                    """UPDATE self_state SET next_heartbeat_at=?, updated_at=?
                       WHERE id=1""",
                    (quiet_end, now),
                )
                return None
            self._db.execute(
                "UPDATE self_state SET heartbeat_claimed_at=? WHERE id=1",
                (now,),
            )
        return dict(row)

    def claim_manual_heartbeat(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._db:
            row = self._db.execute(
                "SELECT heartbeat_claimed_at FROM self_state WHERE id=1"
            ).fetchone()
            if row is None or row["heartbeat_claimed_at"] is not None:
                return False
            self._db.execute(
                """UPDATE self_state SET next_heartbeat_at=?, heartbeat_claimed_at=?,
                   updated_at=? WHERE id=1""",
                (now, now, now),
            )
        return True

    def next_heartbeat_due_at(self, enabled: bool) -> float | None:
        row = self._db.execute(
            """SELECT next_heartbeat_at, pending_reply_expectation FROM self_state
               WHERE id=1 AND heartbeat_claimed_at IS NULL"""
        ).fetchone()
        return (
            float(row["next_heartbeat_at"])
            if row
            and (enabled or str(row["pending_reply_expectation"] or "").strip())
            and row["next_heartbeat_at"] is not None
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
        result: str,
        next_heartbeat_at: float,
        mood_transition: dict[str, object] | None,
        messages: list[ChannelMessage],
        reason: str,
        reply_expectation: str = "",
        draft: TurnDraft | None = None,
        pending_reply_turn_id: str | None = None,
        continue_waiting_for_reply: bool = False,
        reply_initial_interval_seconds: float = 60,
        notification_channel: str = "",
    ) -> None:
        now = time.time()
        current = self.self_state(now)
        with self._db:
            pending = self._db.execute(
                """SELECT pending_reply_turn_id, pending_reply_checks,
                          pending_reply_channel
                   FROM self_state WHERE id=1"""
            ).fetchone()
            pending_reply_is_current = bool(
                pending_reply_turn_id
                and pending
                and pending["pending_reply_turn_id"] == pending_reply_turn_id
            )
            if pending_reply_turn_id and not pending_reply_is_current:
                messages = []
            if pending_reply_is_current:
                if continue_waiting_for_reply:
                    checks = int(pending["pending_reply_checks"] or 0) + 1
                    if checks < 3:
                        next_heartbeat_at = (
                            now + reply_initial_interval_seconds * 2**checks
                        )
                    self._db.execute(
                        """UPDATE self_state SET
                           pending_reply_checks=pending_reply_checks+1 WHERE id=1"""
                    )
                else:
                    self._db.execute(
                        """UPDATE self_state SET pending_reply_turn_id=NULL,
                           pending_reply_expectation='', pending_reply_since=NULL,
                           pending_reply_checks=0, pending_reply_channel=''
                           WHERE id=1"""
                    )
                    self._db.execute(
                        """DELETE FROM notifications
                           WHERE notification_key='heartbeat.reply_followup'
                             AND state='pending'"""
                    )
                    self._db.execute(
                        """UPDATE outbox SET state='failed',
                           last_error='reply_waiting_ended'
                           WHERE state='pending' AND turn_id IN (
                               SELECT turn_id FROM notifications
                               WHERE notification_key='heartbeat.reply_followup'
                           )"""
                    )
            self._apply_mood_transition(mood_transition, now)
            self._apply_goal_mutations(draft, now)
            activity_since = (
                current["activity_since"] if current["activity"] == activity else now
            )
            self._db.execute(
                """UPDATE self_state SET activity=?, activity_result=?, activity_since=?,
                   last_heartbeat_at=?, next_heartbeat_at=?, heartbeat_claimed_at=NULL,
                   updated_at=? WHERE id=1""",
                (
                    activity,
                    result[:2000],
                    activity_since,
                    now,
                    next_heartbeat_at,
                    now,
                ),
            )
            if messages:
                target_channel = (
                    str(pending["pending_reply_channel"] or "")
                    if pending_reply_is_current
                    else notification_channel
                )
                self._db.execute(
                    """INSERT OR IGNORE INTO notifications
                       (id, turn_id, goal_id, notification_key, priority, reason,
                        messages_json, reply_expectation, state, not_before, created_at,
                        target_channel)
                        VALUES (?, ?, 'heartbeat', ?, 'normal', ?, ?, ?,
                               'pending', ?, ?, ?)""",
                    (
                        f"notification:{turn_id}",
                        turn_id,
                        (
                            "heartbeat.reply_followup"
                            if pending_reply_is_current
                            else "heartbeat.chat"
                        ),
                        reason[:500],
                        json.dumps(messages, ensure_ascii=False),
                        reply_expectation,
                        now,
                        now,
                        target_channel,
                    ),
                )
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   failure_reason=NULL, updated_at=? WHERE id=?""",
                (now, turn_id),
            )

    @staticmethod
    def _reflection_slot(
        now: float, timezone: str, at: str
    ) -> tuple[str, float, datetime]:
        zone = ZoneInfo(timezone)
        local = datetime.fromtimestamp(now, zone)
        hour, minute = map(int, at.split(":"))
        scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scheduled.timestamp() > now:
            scheduled -= timedelta(days=1)
        local_date = (scheduled.date() - timedelta(days=1)).isoformat()
        return local_date, scheduled.timestamp(), scheduled

    def claim_due_reflection(
        self,
        config: ReflectionConfig,
        timezone: str,
        now: float | None = None,
    ) -> dict[str, object] | None:
        if not config.enabled:
            return None
        now = time.time() if now is None else now
        local_date, scheduled_at, _ = self._reflection_slot(now, timezone, config.at)
        reflection_id = f"reflection:{local_date}"
        with self._db:
            self._db.execute(
                """INSERT OR IGNORE INTO reflections
                   (id, local_date, state, scheduled_at, created_at)
                   VALUES (?, ?, 'pending', ?, ?)""",
                (reflection_id, local_date, scheduled_at, now),
            )
            row = self._db.execute(
                """SELECT * FROM reflections
                   WHERE id=? AND state='pending' AND claimed_at IS NULL
                     AND scheduled_at<=? AND COALESCE(retry_at, 0)<=?""",
                (reflection_id, now, now),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                """UPDATE reflections SET state='running', claimed_at=?, error=NULL
                   WHERE id=?""",
                (now, reflection_id),
            )
        return dict(row)

    def next_reflection_due_at(
        self,
        config: ReflectionConfig,
        timezone: str,
        now: float | None = None,
    ) -> float | None:
        if not config.enabled:
            return None
        now = time.time() if now is None else now
        local_date, scheduled_at, scheduled = self._reflection_slot(
            now, timezone, config.at
        )
        row = self._db.execute(
            "SELECT state, retry_at FROM reflections WHERE local_date=?",
            (local_date,),
        ).fetchone()
        if row is None:
            return scheduled_at
        if row["state"] == "pending":
            return max(scheduled_at, float(row["retry_at"] or 0))
        if row["state"] == "running":
            return None
        next_scheduled = scheduled + timedelta(days=1)
        return next_scheduled.timestamp()

    def release_reflection(
        self, local_date: str, error: str, delay_seconds: float = 300
    ) -> None:
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE reflections SET state='pending', claimed_at=NULL,
                   retry_at=?, error=? WHERE local_date=? AND state='running'""",
                (now + delay_seconds, error[:500], local_date),
            )

    def reflection_source(
        self, local_date: str, timezone: str, token_budget: int
    ) -> dict[str, object]:
        zone = ZoneInfo(timezone)
        start = datetime.fromisoformat(f"{local_date}T00:00:00").replace(tzinfo=zone)
        end = start + timedelta(days=1)
        entries: list[tuple[float, str, str, bool, bool]] = []
        for row in self._db.execute(
            """SELECT role, content, created_at FROM messages
               WHERE created_at>=? AND created_at<? ORDER BY created_at""",
            (start.timestamp(), end.timestamp()),
        ).fetchall():
            owner = row["role"] == "user"
            entries.append(
                (
                    float(row["created_at"]),
                    "OWNER" if owner else "MOMOI",
                    str(row["content"]),
                    owner,
                    owner,
                )
            )
        for row in self._db.execute(
            """SELECT a.tool_name, a.state, a.ok, a.capability, t.started_at
               FROM tool_audit AS a JOIN turns AS t ON t.id=a.turn_id
               WHERE t.started_at>=? AND t.started_at<? ORDER BY t.started_at""",
            (start.timestamp(), end.timestamp()),
        ).fetchall():
            ok = "unknown" if row["ok"] is None else str(bool(row["ok"])).lower()
            entries.append(
                (
                    float(row["started_at"]),
                    f"TOOL {row['tool_name']}",
                    f"state={row['state']} ok={ok} capability={row['capability']}",
                    False,
                    False,
                )
            )
        for row in self._db.execute(
            """SELECT failure_reason, started_at FROM turns
               WHERE started_at>=? AND started_at<? AND failure_reason IS NOT NULL""",
            (start.timestamp(), end.timestamp()),
        ).fetchall():
            entries.append(
                (
                    float(row["started_at"]),
                    "RUNTIME FAILURE",
                    str(row["failure_reason"]),
                    False,
                    False,
                )
            )
        selected: list[tuple[float, str, str, bool, bool]] = []
        used = 0
        for entry in sorted(entries, reverse=True):
            line = f"[{entry[1]}]\n{entry[2]}"
            size = estimate_tokens(line)
            if selected and used + size > token_budget:
                break
            if not selected and size > token_budget:
                entry = (*entry[:2], entry[2][:token_budget], *entry[3:])
                size = estimate_tokens(f"[{entry[1]}]\n{entry[2]}")
            selected.append(entry)
            used += size
        selected.reverse()
        text = "\n\n".join(
            f"[{label}]\n{content}" for _, label, content, _, _ in selected
        )
        owner_text = "\n".join(content for _, _, content, owner, _ in selected if owner)
        knowledge_text = "\n".join(
            content for _, _, content, _, knowledge in selected if knowledge
        )
        return {
            "text": text,
            "owner_text": owner_text,
            "knowledge_text": knowledge_text,
            "entries": len(selected),
            "start_at": start.timestamp(),
            "end_at": end.timestamp(),
        }

    def commit_reflection(
        self,
        local_date: str,
        turn_id: str,
        summary: str,
        memories: list[dict[str, object]],
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

    def goal(self, goal_id: str) -> dict[str, object] | None:
        row = self._db.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        return self._goal_dict(row) if row else None

    def list_goals(self, include_closed: bool = False) -> list[dict[str, object]]:
        where = (
            "" if include_closed else "WHERE status IN ('active', 'waiting', 'blocked')"
        )
        rows = self._db.execute(
            f"SELECT * FROM goals {where} ORDER BY updated_at DESC"
        ).fetchall()
        return [self._goal_dict(row) for row in rows]

    def commit_goal_draft(self, draft: TurnDraft) -> None:
        with self._db:
            self._apply_goal_mutations(draft, time.time())

    def active_goals_context(self, authority: str | None = None) -> str:
        authority_clause = " AND authority=?" if authority else ""
        rows = self._db.execute(
            f"""SELECT * FROM goals
               WHERE status IN ('active', 'waiting', 'blocked')
               {authority_clause}
               ORDER BY COALESCE(next_review_at, 1e30), updated_at DESC
               LIMIT 20""",
            (authority,) if authority else (),
        ).fetchall()
        if not rows:
            return ""
        lines = []
        for row in rows:
            goal = self._goal_dict(row)
            review = (
                datetime.fromtimestamp(float(goal["next_review_at"]))
                .astimezone()
                .isoformat(timespec="seconds")
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

    def add_emotion(
        self, slug: str, path: str | Path, description: str
    ) -> dict[str, object]:
        slug = slug.strip()
        description = description.strip()
        asset = self._resolve_asset_path(path)
        if not valid_emotion_slug(slug):
            raise ValueError(
                "slug must use lowercase letters, digits, dot, underscore, or hyphen"
            )
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
                if (
                    message["media_path"]
                    and self._resolve_asset_path(str(message["media_path"])).is_file()
                ):
                    self._db.execute(
                        """UPDATE outbox SET state='pending', attempts=0,
                           last_error=NULL, next_attempt_at=0 WHERE id=?""",
                        (message["id"],),
                    )

    def emotion_path_referenced(
        self, path: str, *, exclude_slug: str | None = None
    ) -> bool:
        path = self._stored_asset_path(path)
        return (
            self._db.execute(
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
            ).fetchone()
            is not None
        )

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
        target_channel: str = "",
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
            schedule = (
                json.loads(str(row["schedule_json"])) if row["schedule_json"] else None
            )
            if schedule is not None and config is not None:
                quiet_end = quiet_until(now, config)
                if quiet_end > now:
                    self._db.execute(
                        """UPDATE reminders SET fire_at=?, claimed_at=NULL, updated_at=?
                           WHERE id=?""",
                        (quiet_end, now, reminder_id),
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
                    (next_schedule_at(schedule, now), now, reminder_id),
                )
            reminder_turn_id = f"reminder:{reminder_id}:{occurrence}"
            self._db.execute(
                """INSERT INTO messages
                   (turn_id, role, content, created_at, source_event_ids_json)
                   VALUES (?, 'assistant', ?, ?, ?)""",
                (
                    reminder_turn_id,
                    row["text"],
                    now,
                    json.dumps([f"reminder:{reminder_id}"]),
                ),
            )
            self._db.execute(
                """INSERT OR IGNORE INTO outbox
                   (turn_id, dedupe_key, text, target_channel)
                   VALUES (?, ?, ?, ?)""",
                (
                    reminder_turn_id,
                    reminder_turn_id,
                    row["text"],
                    target_channel,
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
        serialized = json.dumps(
            arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
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

    def queue_progress(
        self,
        turn_id: str,
        tool_call_id: str,
        messages: list[ChannelMessage],
        target_channel: str = "",
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
                       (turn_id, dedupe_key, text, kind, media_path, payload_json,
                        target_channel)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        turn_id,
                        f"turn:{turn_id}:progress:{tool_call_id}:{index}",
                        text,
                        kind,
                        path,
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        target_channel,
                    ),
                )

    def commit_turn(
        self,
        events: list[IncomingMessage],
        user_text: str,
        reply: AgentReply,
        draft: TurnDraft | None = None,
        turn_id: str | None = None,
        target_channel: str = "",
    ) -> str:
        assistant_messages = reply.messages
        normalized_messages = [
            self._outbox_content(message) for message in assistant_messages
        ]
        turn_id = turn_id or uuid.uuid4().hex
        event_ids = [event.event_id for event in events]
        now = time.time()
        with self._db:
            self._db.execute(
                """INSERT INTO messages
                   (turn_id, role, content, created_at, source_event_ids_json)
                   VALUES (?, 'user', ?, ?, ?)""",
                (
                    turn_id,
                    user_text,
                    now,
                    json.dumps(event_ids, ensure_ascii=False),
                ),
            )
            progress = self._db.execute(
                """SELECT text, created_at FROM turn_progress
                   WHERE turn_id=? ORDER BY created_at, tool_call_id, part_index""",
                (turn_id,),
            ).fetchall()
            self._db.executemany(
                """INSERT INTO messages
                   (turn_id, role, content, created_at, source_event_ids_json)
                   VALUES (?, 'assistant', ?, ?, ?)""",
                (
                    (
                        turn_id,
                        row["text"],
                        row["created_at"],
                        json.dumps(event_ids, ensure_ascii=False),
                    )
                    for row in progress
                ),
            )
            for index, (assistant_text, kind, path, payload) in enumerate(
                normalized_messages
            ):
                self._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at, source_event_ids_json)
                       VALUES (?, 'assistant', ?, ?, ?)""",
                    (
                        turn_id,
                        assistant_text,
                        now,
                        json.dumps(event_ids, ensure_ascii=False),
                    ),
                )
                self._db.execute(
                    """INSERT INTO outbox
                       (turn_id, dedupe_key, text, kind, media_path, payload_json,
                        reply_expectation, target_channel)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        turn_id,
                        f"turn:{turn_id}:{index}",
                        assistant_text,
                        kind,
                        path,
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        (
                            reply.reply_expectation
                            if reply.expects_reply
                            and index == len(normalized_messages) - 1
                            else ""
                        ),
                        target_channel,
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
                "UPDATE events SET processed=1 WHERE id=?",
                ((event_id,) for event_id in event_ids),
            )
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   source_ids_json=?, failure_reason=NULL, updated_at=? WHERE id=?""",
                (json.dumps(event_ids, ensure_ascii=False), now, turn_id),
            )
        return turn_id

    def commit_autonomous_turn(
        self,
        goal_id: str,
        draft: TurnDraft,
        turn_id: str | None = None,
        notification_channel: str = "",
    ) -> str:
        turn_id = turn_id or uuid.uuid4().hex
        now = time.time()
        with self._db:
            self._apply_goal_mutations(draft, now)
            self._apply_reminder_mutations(draft, now)
            if goal_id not in draft.goals:
                current = self.goal(goal_id)
                next_review_at = (
                    next_schedule_at(current["schedule"], now)
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
                        messages_json, state, not_before, created_at, target_channel)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
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
                        notification_channel,
                    ),
                )
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   failure_reason=NULL, updated_at=? WHERE id=?""",
                (now, turn_id),
            )
        return turn_id

    def _notification_not_before(
        self, row: sqlite3.Row, config: NotificationConfig, now: float
    ) -> float:
        priority = str(row["priority"])
        eligible = now
        if priority == "normal":
            eligible = max(eligible, quiet_until(now, config))
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
        primary_channel: str = "",
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
            target_channel = str(row["target_channel"] or primary_channel)
            source = (
                f"heartbeat:{row['turn_id']}"
                if row["goal_id"] == "heartbeat"
                else f"goal:{row['goal_id']}"
            )
            for index, message in enumerate(messages):
                visible, kind, path, payload = self._outbox_content(message)
                self._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at, source_event_ids_json)
                       VALUES (?, 'assistant', ?, ?, ?)""",
                    (row["turn_id"], visible, now, json.dumps([source])),
                )
                self._db.execute(
                    """INSERT OR IGNORE INTO outbox
                       (turn_id, dedupe_key, text, kind, media_path, payload_json,
                        reply_expectation, target_channel)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["turn_id"],
                        f"notification:{notification_id}:{index}",
                        visible,
                        kind,
                        path,
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        (
                            row["reply_expectation"]
                            if index == len(messages) - 1
                            else ""
                        ),
                        target_channel,
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
                    goal["id"],
                    goal["title"],
                    goal["success_criteria"],
                    goal["authority"],
                    goal["source_event_id"],
                    goal["status"],
                    json.dumps(goal.get("plan", []), ensure_ascii=False),
                    goal.get("next_action", ""),
                    goal.get("waiting_for", ""),
                    goal.get("blocked_reason", ""),
                    goal.get("latest_result", ""),
                    json.dumps(goal.get("schedule"), ensure_ascii=False)
                    if goal.get("schedule")
                    else "",
                    goal.get("next_review_at"),
                    now,
                    now,
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
