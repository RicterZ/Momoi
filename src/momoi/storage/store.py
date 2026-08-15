import hashlib
import json
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
from ..context_time import context_timestamp
from ..emotions import emotion_slug, valid_emotion_slug
from ..logging_context import log_event, safe_preview
from ..models import (
    AgentReply,
    IncomingMessage,
    TurnDraft,
)
from .delivery import DeliveryStore
from .memory import (
    MemoryStore,
    estimate_tokens,
    excerpt_tokens,
    lexical_units,
    token_chunk,
    truncate_tokens,
)
from .scheduling import next_schedule_at, quiet_until

logger = logging.getLogger(__name__)


BASELINE_MOOD_STATE = "calm"
BASELINE_MOOD_INTENSITY = 0.35
BASELINE_MOOD_CAUSE = "resting baseline"
DEFAULT_ACTIVITY = "spending time freely"
EPISODE_CONSOLIDATION_LOOKBACK_SECONDS = 30 * 24 * 60 * 60
REFLECTION_MEMORY_KINDS = {
    "owner_profile",
    "owner_preference",
    "world_knowledge",
    "self_insight",
    "relationship",
    "shared_experience",
    "practice",
}
def _add_context_timestamps(
    value: dict[str, object], fields: tuple[str, ...]
) -> None:
    for name in fields:
        if value.get(name) is not None:
            value[f"{name.removesuffix('_at')}_timestamp"] = context_timestamp(
                value[name]
            )


class Store(MemoryStore, DeliveryStore):
    def __init__(self, path: Path, workspace: Path | None = None) -> None:
        database = Path(path).expanduser().resolve()
        self._workspace = (workspace or database.parent).expanduser().resolve()
        self._db = sqlite3.connect(database)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self._compact_recall_index_if_pending()
        self._migrate_emotion_paths()
        self._recover_outbox()
        self._recover_webhooks()

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def _message_delivery_state(
        outbox_state: str, possible_duplicate: bool = False
    ) -> str:
        if possible_duplicate and outbox_state != "sent":
            return "uncertain"
        return {
            "sent": "delivered",
            "ambiguous": "uncertain",
            "failed": "failed",
            "superseded": "failed",
        }.get(outbox_state, "queued")

    def _sync_outbox_message(self, outbox_id: int, outbox_state: str) -> None:
        episodes = self._db.execute(
            """SELECT DISTINCT et.episode_id FROM messages AS m
               JOIN episode_turns AS et ON et.turn_id=m.turn_id
               WHERE m.outbox_id=?""",
            (outbox_id,),
        ).fetchall()
        outbox = self._db.execute(
            "SELECT possible_duplicate FROM outbox WHERE id=?", (outbox_id,)
        ).fetchone()
        self._db.execute(
            "UPDATE messages SET delivery_state=? WHERE outbox_id=?",
            (
                self._message_delivery_state(
                    outbox_state,
                    bool(outbox and outbox["possible_duplicate"]),
                ),
                outbox_id,
            ),
        )
        for row in episodes:
            self._reindex_episode_terms(str(row["episode_id"]))

    def _migrate(self) -> None:
        self._db.executescript(Path(__file__).with_name("schema.sql").read_text())
        self._migrate_recall_index()
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
        migrating_legacy_turn_ids = "turn_id" not in message_columns
        if migrating_legacy_turn_ids:
            self._db.execute(
                "ALTER TABLE messages ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''"
            )
        if "outbox_id" not in message_columns:
            self._db.execute("ALTER TABLE messages ADD COLUMN outbox_id INTEGER")
        migrating_legacy_delivery = "delivery_state" not in message_columns
        if migrating_legacy_delivery:
            self._db.execute(
                """ALTER TABLE messages ADD COLUMN delivery_state
                   TEXT NOT NULL DEFAULT 'delivered'"""
            )
        self._db.execute(
            """UPDATE messages SET delivery_state='internal'
               WHERE role='assistant'
                 AND content LIKE '[AUTONOMOUS %; not sent to the owner]%'"""
        )
        for outbox in self._db.execute(
            """SELECT id, turn_id, text, state, possible_duplicate
               FROM outbox ORDER BY id"""
        ).fetchall():
            message = self._db.execute(
                """SELECT id FROM messages
                   WHERE turn_id=? AND role='assistant' AND content=?
                     AND outbox_id IS NULL AND delivery_state<>'internal'
                   ORDER BY id LIMIT 1""",
                (outbox["turn_id"], outbox["text"]),
            ).fetchone()
            if message is None and migrating_legacy_turn_ids:
                message = self._db.execute(
                    """SELECT id FROM messages
                       WHERE role='assistant' AND content=?
                         AND outbox_id IS NULL AND delivery_state<>'internal'
                       ORDER BY id LIMIT 1""",
                    (outbox["text"],),
                ).fetchone()
            if message is not None:
                self._db.execute(
                    "UPDATE messages SET outbox_id=?, delivery_state=? WHERE id=?",
                    (
                        outbox["id"],
                        self._message_delivery_state(
                            str(outbox["state"]),
                            bool(outbox["possible_duplicate"]),
                        ),
                        message["id"],
                    ),
                )
        if migrating_legacy_delivery:
            self._db.execute(
                """UPDATE messages SET delivery_state='uncertain'
                   WHERE role='assistant' AND outbox_id IS NULL
                     AND delivery_state='delivered'"""
            )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS messages_delivery ON messages(delivery_state, outbox_id)"
        )
        self._db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS messages_outbox
               ON messages(outbox_id) WHERE outbox_id IS NOT NULL"""
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
        memory_columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(memories)").fetchall()
        }
        if "activation" not in memory_columns:
            self._db.execute(
                """ALTER TABLE memories ADD COLUMN activation TEXT NOT NULL
                   DEFAULT 'recall'"""
            )
            self._db.execute(
                "UPDATE memories SET activation='always' WHERE kind='preference'"
            )
        self._db.execute(
            """CREATE INDEX IF NOT EXISTS memories_activation
               ON memories(activation, updated_at DESC)
               WHERE superseded_by IS NULL"""
        )
        conflict_columns = {
            str(row["name"])
            for row in self._db.execute(
                "PRAGMA table_info(memory_conflicts)"
            ).fetchall()
        }
        if "activation" not in conflict_columns:
            self._db.execute(
                """ALTER TABLE memory_conflicts ADD COLUMN activation TEXT NOT NULL
                   DEFAULT 'recall'"""
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
        if "mood_settle_at" in self_state_columns:
            self._db.execute("UPDATE self_state SET mood_settle_at=NULL")
        migrating_reply_schedule = (
            "pending_reply_next_check_at" not in self_state_columns
        )
        if "activity_result" not in self_state_columns:
            self._db.execute(
                "ALTER TABLE self_state ADD COLUMN activity_result TEXT NOT NULL DEFAULT ''"
            )
        for name, definition in (
            (
                "heartbeat_claim_kind",
                "TEXT CHECK (heartbeat_claim_kind IN ('ordinary', 'reply', 'manual'))",
            ),
            ("pending_reply_turn_id", "TEXT"),
            ("pending_reply_expectation", "TEXT NOT NULL DEFAULT ''"),
            ("pending_reply_since", "REAL"),
            ("pending_reply_checks", "INTEGER NOT NULL DEFAULT 0"),
            ("pending_reply_last_reason", "TEXT NOT NULL DEFAULT ''"),
            ("pending_reply_channel", "TEXT NOT NULL DEFAULT ''"),
            ("pending_reply_next_check_at", "REAL"),
            ("cooled_reply_expectation", "TEXT NOT NULL DEFAULT ''"),
            ("cooled_reply_source_turn_id", "TEXT NOT NULL DEFAULT ''"),
            ("cooled_reply_since", "REAL"),
            ("cooled_reply_review_at", "REAL"),
            ("cooled_reply_checks", "INTEGER NOT NULL DEFAULT 0"),
            ("cooled_reply_reason", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in self_state_columns:
                self._db.execute(
                    f"ALTER TABLE self_state ADD COLUMN {name} {definition}"
                )
        if migrating_reply_schedule:
            self._db.execute(
                """UPDATE self_state
                   SET pending_reply_next_check_at=next_heartbeat_at,
                       next_heartbeat_at=0
                   WHERE pending_reply_expectation<>''"""
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
        for name, definition in (
            ("superseded_at", "REAL"),
            ("superseded_reason", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in notification_columns:
                self._db.execute(
                    f"ALTER TABLE notifications ADD COLUMN {name} {definition}"
                )
        notification_schema = str(
            self._db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='notifications'"
            ).fetchone()[0]
        )
        if "'superseded'" not in notification_schema:
            self._db.execute("DROP INDEX IF EXISTS notifications_due")
            self._db.execute(
                "ALTER TABLE notifications RENAME TO notifications_legacy_state"
            )
            self._db.execute(
                """CREATE TABLE notifications (
                       id TEXT PRIMARY KEY,
                       turn_id TEXT NOT NULL UNIQUE,
                       goal_id TEXT NOT NULL,
                       notification_key TEXT NOT NULL,
                       priority TEXT NOT NULL CHECK (priority IN ('normal', 'urgent')),
                       reason TEXT NOT NULL,
                       messages_json TEXT NOT NULL,
                       reply_expectation TEXT NOT NULL DEFAULT '',
                       state TEXT NOT NULL CHECK (
                           state IN ('pending', 'queued', 'superseded')
                       ),
                       not_before REAL NOT NULL,
                       claimed_at REAL,
                       created_at REAL NOT NULL,
                       queued_at REAL,
                       superseded_at REAL,
                       superseded_reason TEXT NOT NULL DEFAULT '',
                       target_channel TEXT NOT NULL DEFAULT ''
                   )"""
            )
            self._db.execute(
                """INSERT INTO notifications
                   (id, turn_id, goal_id, notification_key, priority, reason,
                    messages_json, reply_expectation, state, not_before, claimed_at,
                    created_at, queued_at, superseded_at, superseded_reason,
                    target_channel)
                   SELECT id, turn_id, goal_id, notification_key, priority, reason,
                          messages_json, reply_expectation, state, not_before,
                          claimed_at, created_at, queued_at, superseded_at,
                          superseded_reason, target_channel
                   FROM notifications_legacy_state"""
            )
            self._db.execute("DROP TABLE notifications_legacy_state")
            self._db.execute(
                """CREATE INDEX notifications_due ON notifications(not_before)
                   WHERE state='pending'"""
            )
        episode_columns = {
            str(row["name"])
            for row in self._db.execute(
                "PRAGMA table_info(conversation_episodes)"
            ).fetchall()
        }
        if "working_summary_claims_json" not in episode_columns:
            self._db.execute(
                """ALTER TABLE conversation_episodes
                   ADD COLUMN working_summary_claims_json
                   TEXT NOT NULL DEFAULT '[]'"""
            )
            self._db.execute(
                """UPDATE conversation_episodes
                   SET summarized_through_ordinal=0,
                       summary_retry_at=NULL
                   WHERE status IN ('open', 'closing')
                     AND working_summary<>''"""
            )
        for name, definition in (
            ("narrative_summary", "TEXT NOT NULL DEFAULT ''"),
            ("emotional_context_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("outcomes_json", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            if name not in episode_columns:
                self._db.execute(
                    f"ALTER TABLE conversation_episodes ADD COLUMN {name} {definition}"
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
                 AND mood_cause='personality baseline'""",
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
        self._backfill_episode_recall_terms()
        self._db.execute("UPDATE goals SET review_claimed_at=NULL")
        self._db.execute("UPDATE reminders SET claimed_at=NULL WHERE status='pending'")
        self._db.execute(
            "UPDATE notifications SET claimed_at=NULL WHERE state='pending'"
        )
        self._supersede_heartbeat_contacts(
            ("heartbeat.chat", "heartbeat.reply_followup"),
            "process_restart_invalidated_ephemeral_contact",
            now,
        )
        self._db.execute(
            """UPDATE self_state SET heartbeat_claimed_at=NULL,
               heartbeat_claim_kind=NULL WHERE id=1"""
        )
        self._db.execute(
            "UPDATE reflections SET state='pending', claimed_at=NULL WHERE state='running'"
        )
        self._db.execute("UPDATE conversation_episodes SET summary_claimed_at=NULL")
        self._db.commit()

    def _migrate_recall_index(self) -> None:
        columns = {
            str(row["name"])
            for row in self._db.execute(
                "PRAGMA table_info(episode_recall_terms)"
            ).fetchall()
        }
        if columns == {"episode_key", "term_id"}:
            self._create_recall_lookup_indexes()
            return
        if columns != {"episode_id", "term"}:
            raise RuntimeError("unsupported episode recall index schema")

        message_columns = {
            str(row["name"])
            for row in self._db.execute(
                "PRAGMA table_info(episode_message_recall_terms)"
            ).fetchall()
        }
        if message_columns != {"episode_id", "message_id", "term"}:
            raise RuntimeError("unsupported episode message recall index schema")

        started = time.monotonic()
        old_episode_rows = int(
            self._db.execute(
                "SELECT COUNT(*) FROM episode_recall_terms"
            ).fetchone()[0]
        )
        old_message_rows = int(
            self._db.execute(
                "SELECT COUNT(*) FROM episode_message_recall_terms"
            ).fetchone()[0]
        )
        log_event(
            logger,
            logging.INFO,
            "recall_index_migration_start",
            episode_rows=old_episode_rows,
            message_rows=old_message_rows,
        )
        try:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for statement in (
                    "DROP TABLE IF EXISTS episode_recall_terms_v2",
                    "DROP TABLE IF EXISTS episode_message_recall_terms_v2",
                    """CREATE TABLE episode_recall_terms_v2 (
                        episode_key INTEGER NOT NULL,
                        term_id INTEGER NOT NULL,
                        PRIMARY KEY (episode_key, term_id),
                        FOREIGN KEY (episode_key)
                            REFERENCES recall_episode_ids(id) ON DELETE CASCADE,
                        FOREIGN KEY (term_id)
                            REFERENCES recall_terms(id) ON DELETE CASCADE
                    ) WITHOUT ROWID""",
                    """CREATE TABLE episode_message_recall_terms_v2 (
                        episode_key INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        term_id INTEGER NOT NULL,
                        PRIMARY KEY (episode_key, message_id, term_id),
                        FOREIGN KEY (episode_key)
                            REFERENCES recall_episode_ids(id) ON DELETE CASCADE,
                        FOREIGN KEY (message_id)
                            REFERENCES messages(id) ON DELETE CASCADE,
                        FOREIGN KEY (term_id)
                            REFERENCES recall_terms(id) ON DELETE CASCADE
                    ) WITHOUT ROWID""",
                    """INSERT OR IGNORE INTO recall_episode_ids (episode_id)
                       SELECT id FROM conversation_episodes""",
                    """INSERT OR IGNORE INTO recall_terms (term)
                        SELECT term FROM episode_recall_terms
                        UNION
                        SELECT term FROM episode_message_recall_terms""",
                    """INSERT INTO episode_recall_terms_v2 (episode_key, term_id)
                        SELECT rei.id, terms.id
                        FROM episode_recall_terms AS old
                        JOIN recall_episode_ids AS rei
                          ON rei.episode_id=old.episode_id
                        JOIN recall_terms AS terms ON terms.term=old.term""",
                    """INSERT INTO episode_message_recall_terms_v2
                        (episode_key, message_id, term_id)
                        SELECT rei.id, old.message_id, terms.id
                        FROM episode_message_recall_terms AS old
                        JOIN recall_episode_ids AS rei
                          ON rei.episode_id=old.episode_id
                        JOIN recall_terms AS terms ON terms.term=old.term""",
                ):
                    self._db.execute(statement)
                new_episode_rows = int(
                    self._db.execute(
                        "SELECT COUNT(*) FROM episode_recall_terms_v2"
                    ).fetchone()[0]
                )
                new_message_rows = int(
                    self._db.execute(
                        "SELECT COUNT(*) FROM episode_message_recall_terms_v2"
                    ).fetchone()[0]
                )
                if (
                    new_episode_rows != old_episode_rows
                    or new_message_rows != old_message_rows
                ):
                    raise RuntimeError("recall index migration row count mismatch")
                for statement in (
                    "DROP INDEX IF EXISTS episode_recall_terms_lookup",
                    "DROP INDEX IF EXISTS episode_message_recall_terms_lookup",
                    "DROP TABLE episode_recall_terms",
                    "DROP TABLE episode_message_recall_terms",
                    """ALTER TABLE episode_recall_terms_v2
                       RENAME TO episode_recall_terms""",
                    """ALTER TABLE episode_message_recall_terms_v2
                       RENAME TO episode_message_recall_terms""",
                    """CREATE INDEX episode_recall_terms_lookup
                       ON episode_recall_terms(term_id, episode_key)""",
                    """CREATE INDEX episode_message_recall_terms_lookup
                        ON episode_message_recall_terms
                           (term_id, episode_key, message_id)""",
                    """INSERT OR REPLACE INTO schema_metadata (key, value)
                       VALUES ('recall_index_v2_vacuum_pending', '1')""",
                ):
                    self._db.execute(statement)
            except Exception:
                self._db.rollback()
                raise
            else:
                self._db.commit()
        except Exception as error:
            log_event(
                logger,
                logging.ERROR,
                "recall_index_migration_failure",
                error_type=type(error).__name__,
                reason=safe_preview(str(error), 300),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        log_event(
            logger,
            logging.INFO,
            "recall_index_migration_complete",
            episode_rows=old_episode_rows,
            message_rows=old_message_rows,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def _create_recall_lookup_indexes(self) -> None:
        self._db.execute(
            """CREATE INDEX IF NOT EXISTS episode_recall_terms_lookup
               ON episode_recall_terms(term_id, episode_key)"""
        )
        self._db.execute(
            """CREATE INDEX IF NOT EXISTS episode_message_recall_terms_lookup
               ON episode_message_recall_terms
                  (term_id, episode_key, message_id)"""
        )

    def _compact_recall_index_if_pending(self) -> None:
        pending = self._db.execute(
            """SELECT 1 FROM schema_metadata
               WHERE key='recall_index_v2_vacuum_pending'"""
        ).fetchone()
        if pending is None:
            return
        self._db.commit()
        before_bytes = int(
            self._db.execute("PRAGMA page_count").fetchone()[0]
        ) * int(self._db.execute("PRAGMA page_size").fetchone()[0])
        started = time.monotonic()
        log_event(
            logger,
            logging.INFO,
            "recall_index_compaction_start",
            before_bytes=before_bytes,
        )
        try:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.execute("VACUUM")
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.execute(
                """DELETE FROM schema_metadata
                   WHERE key='recall_index_v2_vacuum_pending'"""
            )
            self._db.commit()
        except Exception as error:
            log_event(
                logger,
                logging.ERROR,
                "recall_index_compaction_failure",
                error_type=type(error).__name__,
                reason=safe_preview(str(error), 300),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        after_bytes = int(
            self._db.execute("PRAGMA page_count").fetchone()[0]
        ) * int(self._db.execute("PRAGMA page_size").fetchone()[0])
        log_event(
            logger,
            logging.INFO,
            "recall_index_compaction_complete",
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            saved_bytes=max(0, before_bytes - after_bytes),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def _table_exists(self, name: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            is not None
        )

    def _recover_outbox(self) -> None:
        recovered = self._db.execute(
            "SELECT id FROM outbox WHERE state='sending'"
        ).fetchall()
        self._db.execute(
            """UPDATE outbox
               SET state='ambiguous', possible_duplicate=1, next_attempt_at=0
               WHERE state='sending' AND attempts < 2"""
        )
        self._db.execute(
            """UPDATE outbox SET state='failed', possible_duplicate=1
               WHERE state='sending' AND attempts >= 2"""
        )
        for row in recovered:
            outbox_id = int(row["id"])
            state = self._db.execute(
                "SELECT state FROM outbox WHERE id=?", (outbox_id,)
            ).fetchone()["state"]
            self._sync_outbox_message(outbox_id, str(state))
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
                now = time.time()
                self._cool_active_reply(now, "owner_message_received")
                self._db.execute(
                    """UPDATE self_state SET pending_reply_turn_id=NULL,
                       pending_reply_expectation='', pending_reply_since=NULL,
                       pending_reply_checks=0, pending_reply_last_reason='',
                       pending_reply_channel='',
                       pending_reply_next_check_at=NULL,
                       updated_at=? WHERE id=1""",
                    (now,),
                )
                self._supersede_heartbeat_contacts(
                    ("heartbeat.chat", "heartbeat.reply_followup"),
                    "owner_message_superseded_heartbeat_contact",
                    now,
                )
        return cursor.rowcount == 1

    def _cool_active_reply(self, now: float, reason: str) -> bool:
        row = self._db.execute(
            """SELECT pending_reply_turn_id, pending_reply_expectation
               FROM self_state WHERE id=1"""
        ).fetchone()
        expectation = str(row["pending_reply_expectation"] or "").strip() if row else ""
        if not expectation:
            return False
        self._db.execute(
            """UPDATE self_state SET cooled_reply_expectation=?,
                   cooled_reply_source_turn_id=?, cooled_reply_since=?,
                   cooled_reply_review_at=?, cooled_reply_checks=0,
                   cooled_reply_reason=?, updated_at=? WHERE id=1""",
            (
                expectation,
                str(row["pending_reply_turn_id"] or ""),
                now,
                now + 86400,
                reason[:300],
                now,
            ),
        )
        return True

    def cooled_reply_expectation_context(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        row = self._db.execute(
            """SELECT cooled_reply_expectation, cooled_reply_source_turn_id,
                      cooled_reply_since, cooled_reply_review_at,
                      cooled_reply_checks, cooled_reply_reason
               FROM self_state WHERE id=1"""
        ).fetchone()
        expectation = str(row["cooled_reply_expectation"] or "").strip() if row else ""
        if not expectation:
            return ""
        source_turn = str(row["cooled_reply_source_turn_id"] or "")
        source_rows = self._db.execute(
            """SELECT role, content, created_at, delivery_state FROM messages
               WHERE turn_id=? AND (role='user' OR delivery_state IN ('delivered','uncertain'))
               ORDER BY id""",
            (source_turn,),
        ).fetchall()
        source_messages = [
            {
                "role": str(item["role"]),
                "content": str(item["content"]),
                "delivery_state": str(item["delivery_state"]),
                "timestamp": context_timestamp(item["created_at"]),
            }
            for item in source_rows
        ]
        review_at = float(row["cooled_reply_review_at"] or now)
        return json.dumps(
            {
                "state": "cooled",
                "expected_response": expectation,
                "source_turn": source_turn,
                "source_messages": source_messages,
                "cooled_at": context_timestamp(row["cooled_reply_since"] or now),
                "age_minutes": max(
                    0,
                    int((now - float(row["cooled_reply_since"] or now)) / 60),
                ),
                "cleanup_due": now >= review_at,
                "review_at": context_timestamp(review_at),
                "review_count": int(row["cooled_reply_checks"] or 0),
                "reason": str(row["cooled_reply_reason"] or ""),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _apply_cooled_reply_action(
        self, draft: TurnDraft | None, now: float
    ) -> None:
        if draft is not None and draft.close_reply_expectation:
            self._db.execute(
                """UPDATE self_state SET cooled_reply_expectation='',
                   cooled_reply_source_turn_id='', cooled_reply_since=NULL,
                   cooled_reply_review_at=NULL, cooled_reply_checks=0,
                   cooled_reply_reason='', updated_at=? WHERE id=1""",
                (now,),
            )
            return
        self._db.execute(
            """UPDATE self_state SET cooled_reply_review_at=?,
               cooled_reply_checks=cooled_reply_checks+1, updated_at=?
               WHERE id=1 AND cooled_reply_expectation<>''
                 AND (cooled_reply_review_at IS NULL OR cooled_reply_review_at<=?)""",
            (now + 86400, now, now),
        )

    def _supersede_heartbeat_contacts(
        self, keys: tuple[str, ...], reason: str, now: float
    ) -> None:
        placeholders = ",".join("?" for _ in keys)
        stale = self._db.execute(
            f"""SELECT o.id, o.state FROM outbox AS o
                LEFT JOIN notifications AS n ON n.turn_id=o.turn_id
                WHERE (
                    n.notification_key IN ({placeholders}) OR EXISTS (
                        SELECT 1 FROM turns AS t
                        WHERE t.id=o.turn_id
                          AND t.kind='autonomous'
                          AND t.state='running'
                          AND t.source_ids_json LIKE '%\"heartbeat:%'
                    )
                ) AND o.state IN ('pending', 'ambiguous')""",
            keys,
        ).fetchall()
        for row in stale:
            outbox_id = int(row["id"])
            episodes = self._db.execute(
                """SELECT DISTINCT et.episode_id FROM messages AS m
                   JOIN episode_turns AS et ON et.turn_id=m.turn_id
                   WHERE m.outbox_id=?""",
                (outbox_id,),
            ).fetchall()
            self._db.execute(
                "UPDATE outbox SET state='superseded', last_error=? WHERE id=?",
                (reason, outbox_id),
            )
            if row["state"] == "ambiguous":
                self._sync_outbox_message(outbox_id, "ambiguous")
            else:
                self._db.execute("DELETE FROM messages WHERE outbox_id=?", (outbox_id,))
                for episode in episodes:
                    self._reindex_episode_terms(str(episode["episode_id"]))
        self._db.execute(
            f"""UPDATE notifications AS n
                SET state='superseded', claimed_at=NULL,
                    superseded_at=?, superseded_reason=?
                WHERE notification_key IN ({placeholders})
                  AND state IN ('pending', 'queued')
                  AND (
                      state='pending' OR EXISTS (
                          SELECT 1 FROM outbox AS o
                          WHERE o.turn_id=n.turn_id AND o.state='superseded'
                      )
                  )""",
            (now, reason, *keys),
        )

    def pending_events(self) -> list[IncomingMessage]:
        rows = self._db.execute(
            "SELECT * FROM events WHERE processed=0 ORDER BY received_at, rowid"
        ).fetchall()
        return [self._incoming_message(row) for row in rows]

    def heartbeat_conversation_snapshot(self) -> dict[str, object]:
        revision = int(
            self._db.execute("SELECT COALESCE(MAX(rowid), 0) FROM events").fetchone()[0]
        )
        if self._db.execute(
            "SELECT 1 FROM events WHERE processed=0 LIMIT 1"
        ).fetchone():
            blocked_by = "pending_owner_event"
        elif self._db.execute(
            """SELECT 1 FROM outbox AS o
               JOIN turns AS t ON t.id=o.turn_id
               WHERE t.kind='owner'
                 AND o.state IN ('pending', 'sending', 'ambiguous') LIMIT 1"""
        ).fetchone():
            blocked_by = "owner_reply_in_flight"
        else:
            blocked_by = ""
        return {
            "owner_event_revision": revision,
            "owner_busy": bool(blocked_by),
            "blocked_by": blocked_by,
        }

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

    def complete_background_turn(self, turn_id: str) -> None:
        with self._db:
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   failure_reason=NULL, updated_at=? WHERE id=?""",
                (time.time(), turn_id),
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
        plan["source_event_ids"] = json.loads(str(plan.pop("source_event_ids_json")))
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

    def next_context_plan_revision(self, turn_id: str) -> int:
        row = self._db.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM context_plans WHERE turn_id=?",
            (turn_id,),
        ).fetchone()
        return int(row[0])

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
                    json.dumps(retrieval, ensure_ascii=False, separators=(",", ":")),
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
        episode.pop("overlap", None)
        _add_context_timestamps(
            episode, ("created_at", "updated_at", "closed_at")
        )
        try:
            claims = json.loads(str(episode.pop("working_summary_claims_json")))
        except (json.JSONDecodeError, TypeError):
            claims = []
        episode["working_summary_claims"] = claims if isinstance(claims, list) else []
        try:
            emotional_context = json.loads(str(episode.pop("emotional_context_json")))
        except (json.JSONDecodeError, TypeError):
            emotional_context = {}
        try:
            outcomes = json.loads(str(episode.pop("outcomes_json")))
        except (json.JSONDecodeError, TypeError):
            outcomes = []
        episode["emotional_context"] = (
            emotional_context if isinstance(emotional_context, dict) else {}
        )
        episode["outcomes"] = outcomes if isinstance(outcomes, list) else []
        episode.pop("summary", None)
        for name in ("topics", "entities", "open_loops"):
            episode[name] = json.loads(str(episode.pop(f"{name}_json")))
        return episode

    @staticmethod
    def _recall_terms(*values: object) -> set[str]:
        return lexical_units(
            " ".join(
                json.dumps(value, ensure_ascii=False)
                if isinstance(value, (list, dict))
                else str(value or "")
                for value in values
            )
        )

    def _episode_recall_key(self, episode_id: str) -> int:
        self._db.execute(
            """INSERT OR IGNORE INTO recall_episode_ids (episode_id)
               VALUES (?)""",
            (episode_id,),
        )
        row = self._db.execute(
            "SELECT id FROM recall_episode_ids WHERE episode_id=?",
            (episode_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("episode recall id was not created")
        return int(row["id"])

    def _recall_term_ids(self, terms: set[str], *, create: bool) -> dict[str, int]:
        if not terms:
            return {}
        ordered = sorted(terms)
        if create:
            self._db.executemany(
                "INSERT OR IGNORE INTO recall_terms (term) VALUES (?)",
                ((term,) for term in ordered),
            )
        resolved: dict[str, int] = {}
        for offset in range(0, len(ordered), 500):
            chunk = ordered[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._db.execute(
                f"SELECT id, term FROM recall_terms WHERE term IN ({placeholders})",
                chunk,
            ).fetchall()
            resolved.update({str(row["term"]): int(row["id"]) for row in rows})
        return resolved

    def _index_episode_terms(self, episode_id: str, *values: object) -> None:
        episode_key = self._episode_recall_key(episode_id)
        term_ids = self._recall_term_ids(self._recall_terms(*values), create=True)
        self._db.executemany(
            """INSERT OR IGNORE INTO episode_recall_terms
               (episode_key, term_id) VALUES (?, ?)""",
            ((episode_key, term_id) for term_id in term_ids.values()),
        )

    def _index_episode_message_terms(
        self,
        episode_id: str,
        message_id: int,
        content: str,
        terms: set[str] | None = None,
    ) -> None:
        terms = self._recall_terms(content) if terms is None else terms
        episode_key = self._episode_recall_key(episode_id)
        term_ids = self._recall_term_ids(terms, create=True)
        self._db.executemany(
            """INSERT OR IGNORE INTO episode_message_recall_terms
               (episode_key, message_id, term_id) VALUES (?, ?, ?)""",
            (
                (episode_key, message_id, term_id)
                for term_id in term_ids.values()
            ),
        )

    def _index_turn_episode_terms(self, turn_id: str) -> None:
        messages = self._db.execute(
            """SELECT id, role, content, delivery_state FROM messages
               WHERE turn_id=?""",
            (turn_id,),
        ).fetchall()
        plan_row = self._db.execute(
            """SELECT plan_json FROM context_plans
               WHERE turn_id=? AND state<>'superseded'
               ORDER BY revision DESC LIMIT 1""",
            (turn_id,),
        ).fetchone()
        plan = json.loads(str(plan_row["plan_json"])) if plan_row else {}
        units = {
            str(unit["id"]): unit
            for unit in plan.get("intent_units", [])
            if isinstance(unit, dict) and unit.get("id")
        }
        for episode in self._db.execute(
            """SELECT episode_id, relation, unit_ids_json FROM episode_turns
               WHERE turn_id=?""",
            (turn_id,),
        ).fetchall():
            episode_id = str(episode["episode_id"])
            unit_values = [
                units[unit_id]
                for unit_id in json.loads(str(episode["unit_ids_json"]))
                if unit_id in units
            ]
            unit_terms = self._recall_terms(
                *(
                    value
                    for unit in unit_values
                    for value in (
                        unit.get("text"),
                        unit.get("intent"),
                        unit.get("references"),
                        unit.get("recall_queries"),
                    )
                )
            )
            episode_key = self._episode_recall_key(episode_id)
            self._db.execute(
                """DELETE FROM episode_message_recall_terms
                   WHERE episode_key=? AND message_id IN (
                       SELECT id FROM messages WHERE turn_id=?
                   )""",
                (episode_key, turn_id),
            )
            if unit_terms:
                self._index_episode_terms(episode_id, *unit_values)
            for message in messages:
                if message["role"] == "assistant" and message["delivery_state"] not in {
                    "delivered",
                    "uncertain",
                    "internal",
                }:
                    continue
                content = str(message["content"])
                content_terms = self._recall_terms(content)
                if not unit_terms:
                    indexed_terms = content_terms
                elif message["role"] == "user":
                    indexed_terms = unit_terms
                elif content_terms & unit_terms or episode["relation"] == "primary":
                    indexed_terms = content_terms
                else:
                    continue
                self._index_episode_terms(episode_id, *indexed_terms)
                self._index_episode_message_terms(
                    episode_id, int(message["id"]), content, indexed_terms
                )

    def _reindex_episode_terms(self, episode_id: str) -> None:
        episode = self.episode(episode_id)
        if episode is None:
            return
        episode_key = self._episode_recall_key(episode_id)
        self._db.execute(
            "DELETE FROM episode_recall_terms WHERE episode_key=?", (episode_key,)
        )
        self._db.execute(
            "DELETE FROM episode_message_recall_terms WHERE episode_key=?",
            (episode_key,),
        )
        self._index_episode_terms(
            episode_id,
            episode["title"],
            episode["working_summary"],
            episode["narrative_summary"],
            episode["emotional_context"],
            episode["outcomes"],
            episode["topics"],
            episode["entities"],
            episode["open_loops"],
        )
        turns = self._db.execute(
            "SELECT turn_id FROM episode_turns WHERE episode_id=? ORDER BY ordinal",
            (episode_id,),
        ).fetchall()
        for turn in turns:
            self._index_turn_episode_terms(str(turn["turn_id"]))

    def _ensure_autonomous_episode(
        self,
        episode_key: str,
        turn_id: str,
        title: str,
        now: float,
        *recall_values: object,
    ) -> str:
        episode_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"momoi:autonomous-episode:{episode_key}"
        ).hex
        self._db.execute(
            """INSERT OR IGNORE INTO turns
               (id, kind, source_ids_json, state, started_at, updated_at)
               VALUES (?, 'autonomous', ?, 'running', ?, ?)""",
            (turn_id, json.dumps([episode_key]), now, now),
        )
        self._db.execute(
            """INSERT OR IGNORE INTO conversation_episodes
               (id, title, salience, created_at, updated_at)
               VALUES (?, ?, 0.4, ?, ?)""",
            (episode_id, title[:200], now, now),
        )
        while True:
            successor = self._db.execute(
                """SELECT l.from_episode_id FROM episode_links AS l
                   JOIN conversation_episodes AS e ON e.id=l.from_episode_id
                   WHERE l.to_episode_id=? AND l.kind='continues'
                   ORDER BY e.created_at DESC LIMIT 1""",
                (episode_id,),
            ).fetchone()
            if successor is None:
                break
            episode_id = str(successor["from_episode_id"])
        linked = self._db.execute(
            """SELECT 1 FROM episode_turns
               WHERE episode_id=? AND turn_id=?""",
            (episode_id, turn_id),
        ).fetchone()
        if linked is None:
            episode_id = self._roll_episode(
                episode_id,
                turn_id,
                now,
                json.dumps(recall_values, ensure_ascii=False),
            )
            ordinal = self._db.execute(
                """SELECT COALESCE(MAX(ordinal), 0) + 1 FROM episode_turns
                   WHERE episode_id=?""",
                (episode_id,),
            ).fetchone()[0]
            self._db.execute(
                """INSERT INTO episode_turns
                   (episode_id, turn_id, ordinal, relation, unit_ids_json)
                   VALUES (?, ?, ?, 'primary', '[]')""",
                (episode_id, turn_id, ordinal),
            )
        self._db.execute(
            "UPDATE conversation_episodes SET updated_at=? WHERE id=?",
            (now, episode_id),
        )
        self._index_episode_terms(episode_id, title, *recall_values)
        self._index_turn_episode_terms(turn_id)
        return episode_id

    def _backfill_episode_recall_terms(self) -> None:
        episodes = self._db.execute(
            """SELECT * FROM conversation_episodes AS e
               WHERE NOT EXISTS (
                   SELECT 1 FROM recall_episode_ids AS rei
                   JOIN episode_recall_terms AS rt ON rt.episode_key=rei.id
                   WHERE rei.episode_id=e.id
               )"""
        ).fetchall()
        for row in episodes:
            episode = self._episode_dict(row)
            self._index_episode_terms(
                str(episode["id"]),
                episode["title"],
                episode["working_summary"],
                episode["narrative_summary"],
                episode["emotional_context"],
                episode["outcomes"],
                episode["topics"],
                episode["entities"],
                episode["open_loops"],
            )
        missing_turns = self._db.execute(
            """SELECT DISTINCT et.turn_id
               FROM episode_turns AS et
               JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE NOT EXISTS (
                   SELECT 1 FROM recall_episode_ids AS rei
                   JOIN episode_message_recall_terms AS mrt
                     ON mrt.episode_key=rei.id
                   WHERE rei.episode_id=et.episode_id AND mrt.message_id=m.id
               )"""
        ).fetchall()
        for turn in missing_turns:
            self._index_turn_episode_terms(str(turn["turn_id"]))
        self._db.execute(
            """DELETE FROM recall_terms
               WHERE NOT EXISTS (
                   SELECT 1 FROM episode_recall_terms AS rt
                   WHERE rt.term_id=recall_terms.id
               )
                 AND NOT EXISTS (
                   SELECT 1 FROM episode_message_recall_terms AS mrt
                   WHERE mrt.term_id=recall_terms.id
               )"""
        )

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
            self._index_episode_terms(
                episode_id, title, topics or [], entities or [], open_loops or []
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

    def recent_conversation_messages(
        self,
        turn_limit: int,
        token_budget: int,
        before_timestamp: float | None = None,
    ) -> list[dict[str, object]]:
        if turn_limit <= 0 or token_budget <= 0:
            return []
        turns = self._db.execute(
            """SELECT t.id, t.updated_at FROM turns AS t
               WHERE t.state='completed' AND EXISTS (
                   SELECT 1 FROM messages AS m
                   WHERE m.turn_id=t.id
                     AND (m.role='user' OR m.delivery_state IN ('delivered', 'uncertain'))
               )
                 AND (? IS NULL OR t.updated_at < ?)
               ORDER BY t.updated_at DESC LIMIT ?""",
            (before_timestamp, before_timestamp, turn_limit),
        ).fetchall()
        if not turns:
            return []
        turn_ids = [str(row["id"]) for row in turns]
        placeholders = ",".join("?" for _ in turn_ids)
        rows = self._db.execute(
            f"""SELECT m.id, m.turn_id, m.role, m.content, m.created_at,
                       m.delivery_state
                FROM messages AS m
                WHERE m.turn_id IN ({placeholders})
                  AND (m.role='user' OR m.delivery_state IN ('delivered', 'uncertain'))
                ORDER BY m.id""",
            tuple(turn_ids),
        ).fetchall()
        by_turn: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            item = dict(row)
            item["timestamp"] = context_timestamp(item["created_at"])
            by_turn.setdefault(str(row["turn_id"]), []).append(item)
        selected: list[list[dict[str, object]]] = []
        used = 0
        for turn_id in turn_ids:
            group = by_turn.get(turn_id, [])
            if not group:
                continue
            size = sum(estimate_tokens(str(item["content"])) for item in group)
            if selected and used + size > token_budget:
                break
            if not selected and size > token_budget:
                per_message = max(1, token_budget // len(group))
                for item in group:
                    item["content"] = truncate_tokens(str(item["content"]), per_message)
                size = sum(estimate_tokens(str(item["content"])) for item in group)
            selected.append(group)
            used += size
        return [item for group in reversed(selected) for item in group]

    def list_episode_candidates(
        self, limit: int = 20, *, after: float | None = None
    ) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        rows = self._db.execute(
            """SELECT * FROM conversation_episodes AS e
               WHERE status IN ('open', 'closing')
                 AND (? IS NULL OR COALESCE((
                     SELECT MAX(t.updated_at) FROM episode_turns AS et
                     JOIN turns AS t ON t.id=et.turn_id
                     WHERE et.episode_id=e.id
                 ), e.updated_at)>=?)
               ORDER BY status='open' DESC,
                        COALESCE((
                            SELECT MAX(t.updated_at) FROM episode_turns AS et
                            JOIN turns AS t ON t.id=et.turn_id
                            WHERE et.episode_id=e.id
                        ), 0) DESC,
                        salience DESC, updated_at DESC
               LIMIT ?""",
            (after, after, limit),
        ).fetchall()
        return [self._episode_dict(row) for row in rows]

    def list_episode_directory(
        self, limit: int = 64, *, after: float | None = None
    ) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        rows = self._db.execute(
            """SELECT e.* FROM conversation_episodes AS e
               WHERE ? IS NULL OR COALESCE((
                   SELECT MAX(t.updated_at) FROM episode_turns AS et
                   JOIN turns AS t ON t.id=et.turn_id
                   WHERE et.episode_id=e.id
               ), e.updated_at)>=?
               ORDER BY status='open' DESC, status='closing' DESC,
                        COALESCE((
                            SELECT MAX(t.updated_at) FROM episode_turns AS et
                            JOIN turns AS t ON t.id=et.turn_id
                            WHERE et.episode_id=e.id
                        ), e.updated_at) DESC, salience DESC LIMIT ?""",
            (after, after, limit),
        ).fetchall()
        return [self._episode_dict(row) for row in rows]

    def list_dashboard_conversations(
        self, limit: int = 64
    ) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        rows = self._db.execute(
            """SELECT * FROM conversation_episodes
               ORDER BY updated_at DESC, id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [self._episode_dict(row) for row in rows]

    def open_conversation_inventory(self, limit: int = 64) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        rows = self._db.execute(
            """SELECT e.id, e.status, e.title, e.working_summary, e.open_loops_json,
                      e.updated_at,
                      COALESCE((
                          SELECT MAX(t.updated_at) FROM episode_turns AS et
                          JOIN turns AS t ON t.id=et.turn_id
                          WHERE et.episode_id=e.id
                      ), e.updated_at) AS last_activity_at
               FROM conversation_episodes AS e
               WHERE e.status IN ('open', 'closing')
               ORDER BY e.status='open' DESC, last_activity_at DESC, e.updated_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        inventory: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            try:
                open_loops = json.loads(str(item.pop("open_loops_json")))
            except (json.JSONDecodeError, TypeError):
                open_loops = []
            item["open_loops"] = open_loops if isinstance(open_loops, list) else []
            item["last_activity_timestamp"] = context_timestamp(
                item["last_activity_at"]
            )
            item["updated_timestamp"] = context_timestamp(item.pop("updated_at"))
            inventory.append(item)
        return inventory

    def open_conversation_inventory_context(self) -> str:
        rows = self.open_conversation_inventory()
        if not rows:
            return "No open or closing conversations are stored."
        lines = [
            "Inventory of conversations still marked open or closing. Use episode_id "
            "in conversation_actions to close a thread that is finished or expired. "
            "Leave it unchanged when it may still continue."
        ]
        for row in rows:
            summary = " ".join(str(row["working_summary"] or "").split())[:240]
            loops = json.dumps(row["open_loops"], ensure_ascii=False)
            lines.append(
                f"episode_id={row['id']} status={row['status']} title={row['title']} "
                f"last_activity={row['last_activity_timestamp']} open_loops={loops}"
                + (f" summary={summary}" if summary else "")
            )
        return "\n".join(lines)

    def apply_conversation_actions(
        self, actions: list[dict[str, object]], *, now: float
    ) -> None:
        if not actions:
            return
        for item in actions:
            if item.get("action") != "close":
                continue
            episode_id = str(item["episode_id"])
            row = self._db.execute(
                """SELECT id FROM conversation_episodes
                   WHERE id=? AND status IN ('open', 'closing')""",
                (episode_id,),
            ).fetchone()
            if row is None:
                continue
            self._db.execute(
                """UPDATE conversation_episodes
                   SET status='closed', closed_at=?, open_loops_json='[]',
                       updated_at=?
                   WHERE id=? AND status IN ('open', 'closing')""",
                (now, now, episode_id),
            )
            self._reindex_episode_terms(episode_id)

    def search_episodes(
        self,
        query: str,
        max_results: int,
        *,
        after: float | None = None,
        before: float | None = None,
    ) -> list[dict[str, object]]:
        if max_results <= 0:
            return []
        query_units = lexical_units(query, strict=True)
        time_filter = after is not None or before is not None
        if not query_units:
            rows = self._db.execute(
                """SELECT e.*, COALESCE((
                       SELECT MAX(t.updated_at) FROM episode_turns AS et
                       JOIN turns AS t ON t.id=et.turn_id
                       WHERE et.episode_id=e.id
                   ), e.updated_at) AS last_activity_at
                   FROM conversation_episodes AS e
                   WHERE (? IS NULL AND ? IS NULL) OR EXISTS (
                       SELECT 1 FROM episode_turns AS et
                       JOIN messages AS m ON m.turn_id=et.turn_id
                       WHERE et.episode_id=e.id
                         AND (? IS NULL OR m.created_at>=?)
                         AND (? IS NULL OR m.created_at<?)
                   )""",
                (after, before, after, after, before, before),
            ).fetchall()
            ranked = [(0.0, float(row["last_activity_at"]), row) for row in rows]
            ranked.sort(key=lambda item: item[1], reverse=True)
            results = []
            for _, _, row in ranked[:max_results]:
                episode = self._episode_dict(row)
                episode["last_activity_timestamp"] = context_timestamp(
                    row["last_activity_at"]
                )
                episode["matches"] = []
                results.append(episode)
            return results
        query_term_ids = tuple(
            self._recall_term_ids(query_units, create=False).values()
        )
        if not query_term_ids:
            return []
        placeholders = ",".join("?" for _ in query_term_ids)
        ranked: list[
            tuple[float, float, sqlite3.Row, list[dict[str, object]]]
        ] = []
        rows = self._db.execute(
            f"""SELECT e.*, matched.overlap, COALESCE((
                       SELECT MAX(t.updated_at) FROM episode_turns AS et
                       JOIN turns AS t ON t.id=et.turn_id
                       WHERE et.episode_id=e.id
                   ), e.updated_at) AS last_activity_at
                FROM conversation_episodes AS e
                JOIN (
                    SELECT rei.episode_id, COUNT(*) AS overlap
                    FROM episode_recall_terms AS rt
                    JOIN recall_episode_ids AS rei ON rei.id=rt.episode_key
                    WHERE rt.term_id IN ({placeholders})
                    GROUP BY rt.episode_key
                ) AS matched ON matched.episode_id=e.id""",
            query_term_ids,
        ).fetchall()
        for row in rows:
            episode_id = str(row["id"])
            if time_filter and self._db.execute(
                """SELECT 1 FROM episode_turns AS et
                   JOIN messages AS m ON m.turn_id=et.turn_id
                   WHERE et.episode_id=?
                     AND (? IS NULL OR m.created_at>=?)
                     AND (? IS NULL OR m.created_at<?)
                   LIMIT 1""",
                (episode_id, after, after, before, before),
            ).fetchone() is None:
                continue
            matches = self._episode_match_snippets(
                episode_id, query_units, after=after, before=before
            )
            metadata_overlap = query_units & lexical_units(
                " ".join(
                    str(row[name] or "")
                    for name in (
                        "title",
                        "working_summary",
                        "topics_json",
                        "entities_json",
                        "open_loops_json",
                    )
                ),
                strict=True,
            )
            if time_filter and not matches and not metadata_overlap:
                continue
            overlap = int(row["overlap"])
            if overlap / max(1, len(query_units)) < 0.1:
                continue
            score = overlap / len(query_units) + float(row["salience"]) * 0.1
            ranked.append((score, float(row["last_activity_at"]), row, matches))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        results: list[dict[str, object]] = []
        for _, _, row, matches in ranked[:max_results]:
            episode = self._episode_dict(row)
            episode["last_activity_timestamp"] = context_timestamp(
                row["last_activity_at"]
            )
            episode["matches"] = matches
            results.append(episode)
        return results

    def _episode_match_snippets(
        self,
        episode_id: str,
        query_units: set[str],
        limit: int = 4,
        *,
        after: float | None = None,
        before: float | None = None,
    ) -> list[dict[str, object]]:
        query_term_ids = tuple(
            self._recall_term_ids(query_units, create=False).values()
        )
        if not query_term_ids:
            return []
        placeholders = ",".join("?" for _ in query_term_ids)
        rows = self._db.execute(
            f"""SELECT m.id, m.turn_id, et.ordinal, m.role, m.content,
                       m.created_at, m.delivery_state, COUNT(*) AS overlap
                FROM episode_message_recall_terms AS mrt
                JOIN recall_episode_ids AS rei ON rei.id=mrt.episode_key
                JOIN messages AS m ON m.id=mrt.message_id
                JOIN episode_turns AS et
                  ON et.episode_id=rei.episode_id AND et.turn_id=m.turn_id
                WHERE rei.episode_id=? AND mrt.term_id IN ({placeholders})
                  AND (m.role='user' OR m.delivery_state IN
                       ('delivered', 'uncertain', 'internal'))
                  AND (? IS NULL OR m.created_at>=?)
                  AND (? IS NULL OR m.created_at<?)
                GROUP BY m.id, m.turn_id, et.ordinal, m.role, m.content, m.created_at
                ORDER BY overlap DESC, et.ordinal DESC
                LIMIT ?""",
            (
                episode_id,
                *query_term_ids,
                after,
                after,
                before,
                before,
                limit,
            ),
        ).fetchall()
        return [
            {
                **{
                    name: row[name]
                    for name in (
                        "id",
                        "turn_id",
                        "ordinal",
                        "role",
                        "created_at",
                        "delivery_state",
                    )
                },
                "timestamp": context_timestamp(row["created_at"]),
                "content": excerpt_tokens(str(row["content"]), query_units, 500),
            }
            for row in rows
        ]

    def conversation_message(
        self,
        episode_id: str,
        message_id: int,
        content_offset: int = 0,
        token_budget: int = 30000,
    ) -> dict[str, object] | None:
        row = self._db.execute(
            """SELECT m.id, m.turn_id, et.ordinal, m.role, m.content, m.created_at,
                      m.delivery_state
               FROM episode_turns AS et
               JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE et.episode_id=? AND m.id=?""",
            (episode_id, message_id),
        ).fetchone()
        if row is None:
            return None
        content, next_offset = token_chunk(
            str(row["content"]), content_offset, token_budget
        )
        return {
            **{
                name: row[name]
                for name in (
                    "id",
                    "turn_id",
                    "ordinal",
                    "role",
                    "created_at",
                    "delivery_state",
                )
            },
            "timestamp": context_timestamp(row["created_at"]),
            "content": content,
            "content_offset": content_offset,
            "next_content_offset": next_offset,
        }

    def conversation_episode(
        self,
        episode_id: str,
        token_budget: int = 30000,
        *,
        before_ordinal: int | None = None,
        after: float | None = None,
        before: float | None = None,
    ) -> dict[str, object] | None:
        episode = self.episode(episode_id)
        if episode is None:
            return None
        archived = self._db.execute(
            """SELECT et.ordinal, m.content FROM episode_turns AS et
               JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE et.episode_id=?
                 AND (? IS NULL OR et.ordinal<?)
                 AND (? IS NULL OR m.created_at>=?)
                 AND (? IS NULL OR m.created_at<?)""",
            (
                episode_id,
                before_ordinal,
                before_ordinal,
                after,
                after,
                before,
                before,
            ),
        ).fetchall()
        messages = self.episode_messages(
            episode_id,
            token_budget,
            before_ordinal=before_ordinal,
            include_nondelivered=True,
            after=after,
            before=before,
        )
        omitted_messages = len(messages) < len(archived)
        content_truncated = (
            sum(estimate_tokens(str(row["content"])) for row in archived) > token_budget
        )
        next_before_ordinal = (
            min(int(message["ordinal"]) for message in messages)
            if omitted_messages and messages
            else None
        )
        return {
            **episode,
            "messages": messages,
            "truncated": omitted_messages or content_truncated,
            "next_before_ordinal": next_before_ordinal,
            "window_first_timestamp": min(
                (str(message["timestamp"]) for message in messages),
                default=None,
            ),
            "window_last_timestamp": max(
                (str(message["timestamp"]) for message in messages),
                default=None,
            ),
        }

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
            inserted = False
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
                inserted = True
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
                """UPDATE conversation_episodes
                   SET status=CASE WHEN ?='primary' THEN 'open' ELSE status END,
                       closed_at=CASE WHEN ?='primary' THEN NULL ELSE closed_at END,
                       updated_at=? WHERE id=?""",
                (relation, relation, now, episode_id),
            )
            if inserted and self._reorder_episode_turns(episode_id, now):
                ordinal = int(
                    self._db.execute(
                        """SELECT ordinal FROM episode_turns
                           WHERE episode_id=? AND turn_id=?""",
                        (episode_id, turn_id),
                    ).fetchone()["ordinal"]
                )
                self._reindex_episode_terms(episode_id)
            else:
                self._index_turn_episode_terms(turn_id)
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

    def episode_messages(
        self,
        episode_id: str,
        token_budget: int,
        *,
        after_ordinal: int = 0,
        before_ordinal: int | None = None,
        exclude_message_ids: set[int] | None = None,
        include_nondelivered: bool = False,
        after: float | None = None,
        before: float | None = None,
    ) -> list[dict[str, object]]:
        if token_budget <= 0:
            return []
        rows = self._db.execute(
            """SELECT m.id, m.turn_id, et.ordinal, m.role, m.content, m.created_at,
                      m.delivery_state
               FROM episode_turns AS et
               JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE et.episode_id=? AND et.ordinal>?
                 AND (? IS NULL OR et.ordinal<?)
                 AND (? IS NULL OR m.created_at>=?)
                 AND (? IS NULL OR m.created_at<?)
                 AND (? OR m.role='user' OR m.delivery_state IN
                      ('delivered', 'uncertain', 'internal'))
               ORDER BY et.ordinal DESC, m.id""",
            (
                episode_id,
                after_ordinal,
                before_ordinal,
                before_ordinal,
                after,
                after,
                before,
                before,
                int(include_nondelivered),
            ),
        ).fetchall()
        excluded = exclude_message_ids or set()
        groups: list[list[dict[str, object]]] = []
        for row in rows:
            item = dict(row)
            item["timestamp"] = context_timestamp(item["created_at"])
            if int(item["id"]) in excluded:
                continue
            if not groups or groups[-1][0]["turn_id"] != item["turn_id"]:
                groups.append([])
            groups[-1].append(item)
        selected: list[list[dict[str, object]]] = []
        used = 0
        for group in groups:
            size = sum(estimate_tokens(str(item["content"])) for item in group)
            if selected and used + size > token_budget:
                break
            if not selected and size > token_budget:
                if len(group) > token_budget:
                    group = (
                        [group[0]]
                        if token_budget == 1
                        else [group[0], *group[-(token_budget - 1) :]]
                    )
                per_message = max(1, token_budget // len(group))
                for item in group:
                    content, next_offset = token_chunk(
                        str(item["content"]), 0, per_message
                    )
                    item["content"] = content
                    item["content_offset"] = 0
                    item["next_content_offset"] = next_offset
                size = sum(estimate_tokens(str(item["content"])) for item in group)
            selected.append(group)
            used += size
        return [item for group in reversed(selected) for item in group]

    def claim_episode_consolidation_candidate(
        self, limit: int = 6
    ) -> dict[str, object] | None:
        rows = self._db.execute(
            """SELECT pending.id, pending.updated_at FROM (
                   SELECT t.id, t.updated_at FROM turns AS t
                   WHERE t.kind='owner' AND t.state='completed'
                     AND NOT EXISTS (
                         SELECT 1 FROM episode_turns AS et WHERE et.turn_id=t.id
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM episode_consolidation_decisions AS d
                         WHERE d.turn_id=t.id
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM messages AS m
                         WHERE m.turn_id=t.id AND m.delivery_state='queued'
                     )
                     AND EXISTS (
                         SELECT 1 FROM messages AS m WHERE m.turn_id=t.id
                     )
                   ORDER BY t.updated_at DESC LIMIT ?
               ) AS pending
               ORDER BY pending.updated_at""",
            (max(1, limit),),
        ).fetchall()
        if not rows:
            return None
        turn_ids = [str(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in turn_ids)
        messages = self._db.execute(
            f"""SELECT id, turn_id, role, content, created_at, delivery_state
                FROM messages
                WHERE turn_id IN ({placeholders})
                  AND (role='user' OR delivery_state IN
                       ('delivered', 'uncertain', 'internal'))
                ORDER BY id""",
            tuple(turn_ids),
        ).fetchall()
        by_turn: dict[str, list[dict[str, object]]] = {
            turn_id: [] for turn_id in turn_ids
        }
        for row in messages:
            item = dict(row)
            item["timestamp"] = context_timestamp(item["created_at"])
            by_turn[str(row["turn_id"])].append(item)
        return {
            "turns": [
                {
                    "turn_id": turn_id,
                    "timestamp": context_timestamp(row["updated_at"]),
                    "messages": by_turn[turn_id],
                }
                for turn_id, row in zip(turn_ids, rows, strict=True)
            ],
            "candidate_episodes": [
                {
                    "id": episode["id"],
                    "title": episode["title"],
                    "status": episode["status"],
                    "narrative_summary": episode["narrative_summary"],
                    "topics": episode["topics"],
                    "entities": episode["entities"],
                    "open_loops": episode["open_loops"],
                }
                for episode in self.list_episode_directory(
                    12, after=time.time() - EPISODE_CONSOLIDATION_LOOKBACK_SECONDS
                )
            ],
        }

    def apply_episode_consolidation(
        self,
        turn_ids: list[str],
        decisions: list[dict[str, object]],
        candidate_episode_ids: list[str] | None = None,
    ) -> tuple[int, int]:
        expected = set(turn_ids)
        allowed_episodes = set(candidate_episode_ids or [])
        if not expected or len(expected) != len(turn_ids):
            raise ValueError("invalid consolidation turn coverage")
        covered: set[str] = set()
        now = time.time()
        linked = 0
        deferred = 0
        touched_episodes: set[str] = set()
        with self._db:
            for decision in decisions:
                if not isinstance(decision, dict):
                    raise ValueError("invalid consolidation decision")
                action = str(decision.get("action") or "")
                expected_keys = {
                    "defer": {"action", "turn_ids", "reason"},
                    "ignore": {"action", "turn_ids", "reason"},
                    "continue": {
                        "action",
                        "episode_id",
                        "turn_ids",
                        "topics",
                        "entities",
                        "open_loops",
                        "salience",
                    },
                    "new": {
                        "action",
                        "key",
                        "title",
                        "turn_ids",
                        "topics",
                        "entities",
                        "open_loops",
                        "salience",
                    },
                }.get(action)
                if expected_keys is None or set(decision) != expected_keys:
                    raise ValueError("invalid consolidation decision")
                raw_turns = decision["turn_ids"]
                if not isinstance(raw_turns, list) or any(
                    not isinstance(value, str) for value in raw_turns
                ):
                    raise ValueError("invalid consolidation turn coverage")
                decision_turns = [str(value) for value in raw_turns]
                if (
                    not decision_turns
                    or len(decision_turns) != len(set(decision_turns))
                    or not set(decision_turns) <= expected
                    or covered & set(decision_turns)
                ):
                    raise ValueError("invalid consolidation turn coverage")
                covered.update(decision_turns)
                if action == "defer":
                    if decision_turns != [turn_ids[-1]]:
                        raise ValueError("only latest consolidation turn may defer")
                    deferred += 1
                    continue
                if action == "ignore":
                    if turn_ids[-1] in decision_turns:
                        raise ValueError("latest consolidation turn may not be ignored")
                    self._db.executemany(
                        """INSERT INTO episode_consolidation_decisions
                           (turn_id, action, reason, processed_at)
                           VALUES (?, 'ignored', ?, ?)""",
                        (
                            (turn_id, str(decision.get("reason") or "")[:500], now)
                            for turn_id in decision_turns
                        ),
                    )
                    continue
                topics = self._consolidation_strings(
                    decision["topics"], "topics", 12, 200
                )
                entities = self._consolidation_strings(
                    decision["entities"], "entities", 20, 200
                )
                loops = self._consolidation_strings(
                    decision["open_loops"], "open loops", 8, 500
                )
                salience = decision["salience"]
                if (
                    isinstance(salience, bool)
                    or not isinstance(salience, (int, float))
                    or not 0 <= float(salience) <= 1
                ):
                    raise ValueError("invalid consolidation salience")
                if action == "continue":
                    episode_id = str(decision["episode_id"])
                    if (
                        episode_id not in allowed_episodes
                        or self.episode(episode_id) is None
                    ):
                        raise ValueError("unknown consolidation episode")
                else:
                    key = str(decision["key"])
                    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", key):
                        raise ValueError("invalid consolidation episode key")
                    episode_id = uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"momoi:consolidated-episode:{decision_turns[0]}:{key}",
                    ).hex
                    title = str(decision["title"]).strip()
                    if not title or len(title) > 200:
                        raise ValueError("invalid consolidation title")
                raw_text = "\n".join(
                    str(row["content"])
                    for turn_id in decision_turns
                    for row in self._db.execute(
                        """SELECT content FROM messages
                           WHERE turn_id=? ORDER BY id""",
                        (turn_id,),
                    ).fetchall()
                )
                existing = self.episode(episode_id)
                if existing is not None:
                    episode_id = self._roll_episode(
                        episode_id,
                        decision_turns[0],
                        now,
                        raw_text,
                        incoming_turns=len(decision_turns),
                    )
                    existing = self.episode(episode_id)
                status = "open" if loops else "closing"
                if existing is None:
                    self._db.execute(
                        """INSERT INTO conversation_episodes
                           (id, status, title, topics_json, entities_json,
                            open_loops_json, salience, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            episode_id,
                            status,
                            title,
                            json.dumps(topics, ensure_ascii=False),
                            json.dumps(entities, ensure_ascii=False),
                            json.dumps(loops, ensure_ascii=False),
                            float(salience),
                            now,
                            now,
                        ),
                    )
                else:
                    merged_topics = list(
                        dict.fromkeys([*existing["topics"], *topics])
                    )[:12]
                    merged_entities = list(
                        dict.fromkeys([*existing["entities"], *entities])
                    )[:20]
                    self._db.execute(
                        """UPDATE conversation_episodes
                           SET topics_json=?, entities_json=?,
                               open_loops_json=?, salience=MAX(salience, ?),
                               status=?, closed_at=NULL, updated_at=?
                           WHERE id=?""",
                        (
                            json.dumps(merged_topics, ensure_ascii=False),
                            json.dumps(merged_entities, ensure_ascii=False),
                            json.dumps(loops, ensure_ascii=False),
                            float(salience),
                            status,
                            now,
                            episode_id,
                        ),
                    )
                for turn_id in decision_turns:
                    ordinal = int(
                        self._db.execute(
                            """SELECT COALESCE(MAX(ordinal), 0) + 1
                               FROM episode_turns WHERE episode_id=?""",
                            (episode_id,),
                        ).fetchone()[0]
                    )
                    self._db.execute(
                        """INSERT INTO episode_turns
                           (episode_id, turn_id, ordinal, relation, unit_ids_json)
                           VALUES (?, ?, ?, 'primary', '[]')""",
                        (episode_id, turn_id, ordinal),
                    )
                    self._db.execute(
                        """INSERT INTO episode_consolidation_decisions
                           (turn_id, action, episode_id, reason, processed_at)
                           VALUES (?, 'linked', ?, '', ?)""",
                        (turn_id, episode_id, now),
                    )
                    self._index_turn_episode_terms(turn_id)
                    linked += 1
                touched_episodes.add(episode_id)
            if covered != expected:
                raise ValueError("incomplete consolidation turn coverage")
            for episode_id in touched_episodes:
                self._reorder_episode_turns(episode_id, now)
                self._reindex_episode_terms(episode_id)
        return linked, deferred

    @staticmethod
    def _consolidation_strings(
        value: object, name: str, maximum: int, max_length: int
    ) -> list[str]:
        if (
            not isinstance(value, list)
            or len(value) > maximum
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item.strip()) > max_length
                for item in value
            )
        ):
            raise ValueError(f"invalid consolidation {name}")
        return [str(item).strip() for item in value]

    def claim_episode_annealing_candidate(
        self, raw_tail_turns: int, raw_token_budget: int
    ) -> dict[str, object] | None:
        raw_tail_turns = max(1, raw_tail_turns)
        raw_token_budget = max(1, raw_token_budget)
        now = time.time()
        with self._db:
            episodes = self._db.execute(
                """SELECT * FROM conversation_episodes
                   WHERE summary_claimed_at IS NULL
                     AND COALESCE(summary_retry_at, 0)<=?
                     AND NOT EXISTS (
                         SELECT 1 FROM episode_turns AS et
                         JOIN messages AS m ON m.turn_id=et.turn_id
                         WHERE et.episode_id=conversation_episodes.id
                           AND m.delivery_state='queued'
                     )
                   ORDER BY updated_at""",
                (now,),
            ).fetchall()
            for episode in episodes:
                rows = self._db.execute(
                    """SELECT et.ordinal, m.id, m.turn_id, m.role, m.content,
                              m.created_at, m.delivery_state
                       FROM episode_turns AS et
                       JOIN messages AS m ON m.turn_id=et.turn_id
                       WHERE et.episode_id=? AND et.ordinal>?
                         AND (m.role='user' OR m.delivery_state IN
                              ('delivered', 'uncertain', 'internal'))
                       ORDER BY et.ordinal, m.id""",
                    (
                        episode["id"],
                        episode["summarized_through_ordinal"],
                    ),
                ).fetchall()
                ordinals = list(dict.fromkeys(int(row["ordinal"]) for row in rows))
                tail_turns = raw_tail_turns if episode["status"] != "closed" else 0
                if not rows and episode["narrative_summary"]:
                    continue
                if not rows and episode["working_summary_claims_json"] != "[]":
                    cursor = self._db.execute(
                        """UPDATE conversation_episodes SET summary_claimed_at=?
                           WHERE id=? AND summary_claimed_at IS NULL""",
                        (now, episode["id"]),
                    )
                    if cursor.rowcount == 1:
                        return {
                            "episode": self._episode_dict(episode),
                            "through_ordinal": int(
                                episode["summarized_through_ordinal"]
                            ),
                            "messages": [],
                        }
                    continue
                if len(ordinals) <= tail_turns:
                    continue
                tokens = sum(estimate_tokens(str(row["content"])) for row in rows)
                if (
                    episode["status"] != "closed"
                    and len(ordinals) <= raw_tail_turns * 2
                    and tokens <= math.ceil(raw_token_budget * 1.25)
                ):
                    continue
                compact: list[dict[str, object]] = []
                compact_tokens = 0
                through = 0
                selected_ordinals = (
                    ordinals[:-tail_turns] if tail_turns else ordinals
                )
                for ordinal in selected_ordinals:
                    group = [row for row in rows if int(row["ordinal"]) == ordinal]
                    group_tokens = sum(
                        estimate_tokens(str(row["content"])) for row in group
                    )
                    if group_tokens > raw_token_budget and not compact:
                        break
                    if compact and compact_tokens + group_tokens > raw_token_budget:
                        break
                    for row in group:
                        item = dict(row)
                        item["timestamp"] = context_timestamp(item["created_at"])
                        compact.append(item)
                    compact_tokens += group_tokens
                    through = ordinal
                if not compact:
                    continue
                cursor = self._db.execute(
                    """UPDATE conversation_episodes SET summary_claimed_at=?
                       WHERE id=? AND summary_claimed_at IS NULL""",
                    (now, episode["id"]),
                )
                if cursor.rowcount != 1:
                    continue
                return {
                    "episode": self._episode_dict(episode),
                    "through_ordinal": through,
                    "messages": compact,
                }
        return None

    def finish_episode_annealing(
        self,
        episode_id: str,
        through_ordinal: int,
        claims: list[object],
        *,
        narrative_summary: str = "",
        emotional_context: dict[str, object] | None = None,
        outcomes: list[object] | None = None,
    ) -> str:
        if not 1 <= len(claims) <= 64:
            raise ValueError("episode summary needs 1 to 64 evidence claims")
        normalized: list[dict[str, object]] = []
        seen: set[tuple[int, str]] = set()
        for claim in claims:
            if not isinstance(claim, dict) or set(claim) != {
                "message_id",
                "turn_id",
                "ordinal",
                "quote",
            }:
                raise ValueError("invalid episode summary claim")
            message_id = claim["message_id"]
            ordinal = claim["ordinal"]
            if (
                isinstance(message_id, bool)
                or not isinstance(message_id, int)
                or isinstance(ordinal, bool)
                or not isinstance(ordinal, int)
                or not isinstance(claim["turn_id"], str)
                or not isinstance(claim["quote"], str)
            ):
                raise ValueError("invalid episode summary citation")
            quote = str(claim["quote"]).strip()
            if not quote or len(quote) > 1000:
                raise ValueError("invalid episode summary quote")
            row = self._db.execute(
                """SELECT m.turn_id, et.ordinal, m.role, m.content,
                          m.delivery_state
                   FROM episode_turns AS et
                   JOIN messages AS m ON m.turn_id=et.turn_id
                   WHERE et.episode_id=? AND m.id=?""",
                (episode_id, message_id),
            ).fetchone()
            if (
                row is None
                or str(row["turn_id"]) != claim["turn_id"]
                or int(row["ordinal"]) != ordinal
                or ordinal > through_ordinal
                or quote not in str(row["content"])
                or (
                    row["role"] == "assistant"
                    and row["delivery_state"]
                    not in {"delivered", "uncertain", "internal"}
                )
            ):
                raise ValueError("episode summary evidence does not match raw history")
            key = (message_id, quote)
            if key in seen:
                raise ValueError("duplicate episode summary claim")
            seen.add(key)
            normalized.append(
                {
                    "message_id": message_id,
                    "turn_id": str(row["turn_id"]),
                    "ordinal": int(row["ordinal"]),
                    "role": str(row["role"]),
                    "delivery_state": str(row["delivery_state"]),
                    "quote": quote,
                }
            )
        lines = []
        for claim in normalized:
            if claim["role"] == "user":
                source = "OWNER"
            elif claim["delivery_state"] == "uncertain":
                source = "MOMOI delivery=uncertain"
            elif claim["delivery_state"] == "internal":
                source = "MOMOI visibility=internal"
            else:
                source = "MOMOI delivery=delivered"
            lines.append(
                f"- [source {source} turn={claim['turn_id']} "
                f"ordinal={claim['ordinal']}] "
                f"{json.dumps(claim['quote'], ensure_ascii=False)}"
            )
        working_summary = "\n".join(lines)
        if len(working_summary) > 12000:
            raise ValueError("episode summary exceeds storage budget")
        narrative_summary = narrative_summary.strip()
        if len(narrative_summary) > 800:
            raise ValueError("episode narrative exceeds storage budget")
        emotional_context = emotional_context or {}
        if (
            not isinstance(emotional_context, dict)
            or set(emotional_context) - {"owner", "momoi", "tone"}
            or any(
                not isinstance(value, str) or len(value.strip()) > 300
                for value in emotional_context.values()
            )
        ):
            raise ValueError("invalid episode emotional context")
        outcomes = outcomes or []
        if (
            not isinstance(outcomes, list)
            or len(outcomes) > 12
            or any(
                not isinstance(value, str)
                or not value.strip()
                or len(value.strip()) > 500
                for value in outcomes
            )
        ):
            raise ValueError("invalid episode outcomes")
        with self._db:
            cursor = self._db.execute(
                """UPDATE conversation_episodes
                   SET working_summary=?, working_summary_claims_json=?,
                       narrative_summary=?, emotional_context_json=?,
                       outcomes_json=?,
                       summarized_through_ordinal=?,
                       summary_claimed_at=NULL, summary_retry_at=NULL,
                       summary_failure_count=0, updated_at=?
                   WHERE id=? AND summary_claimed_at IS NOT NULL
                     AND summarized_through_ordinal<=?""",
                (
                    working_summary,
                    json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                    narrative_summary,
                    json.dumps(
                        {
                            key: str(value).strip()
                            for key, value in emotional_context.items()
                            if str(value).strip()
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        [str(value).strip() for value in outcomes],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    through_ordinal,
                    time.time(),
                    episode_id,
                    through_ordinal,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("claimed episode summary was not found")
            self._reindex_episode_terms(episode_id)
        return working_summary

    def release_episode_annealing(
        self, episode_id: str, *, failed: bool = True
    ) -> None:
        with self._db:
            if not failed:
                self._db.execute(
                    """UPDATE conversation_episodes
                       SET summary_claimed_at=NULL
                       WHERE id=? AND summary_claimed_at IS NOT NULL""",
                    (episode_id,),
                )
                return
            row = self._db.execute(
                """SELECT summary_failure_count FROM conversation_episodes
                   WHERE id=? AND summary_claimed_at IS NOT NULL""",
                (episode_id,),
            ).fetchone()
            if row is None:
                return
            failures = int(row["summary_failure_count"]) + 1
            delay = min(3600, 60 * 2 ** min(failures - 1, 6))
            self._db.execute(
                """UPDATE conversation_episodes
                   SET summary_claimed_at=NULL, summary_retry_at=?,
                       summary_failure_count=? WHERE id=?""",
                (time.time() + delay, failures, episode_id),
            )

    def next_episode_annealing_retry_at(self) -> float | None:
        row = self._db.execute(
            """SELECT MIN(summary_retry_at) AS due
               FROM conversation_episodes
               WHERE summary_claimed_at IS NULL
                 AND summary_retry_at IS NOT NULL"""
        ).fetchone()
        return float(row["due"]) if row and row["due"] is not None else None

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

    @staticmethod
    def _episode_title(text: str, fallback: str) -> str:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("["):
                return line[:200]
        return fallback

    def _episode_size(self, episode_id: str) -> tuple[int, int]:
        turns = int(
            self._db.execute(
                "SELECT COUNT(*) FROM episode_turns WHERE episode_id=?",
                (episode_id,),
            ).fetchone()[0]
        )
        messages = self._db.execute(
            """SELECT m.content FROM episode_turns AS et
               JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE et.episode_id=?""",
            (episode_id,),
        ).fetchall()
        return turns, sum(
            estimate_tokens(str(message["content"])) for message in messages
        )

    def _reorder_episode_turns(self, episode_id: str, now: float) -> bool:
        rows = self._db.execute(
            """SELECT et.turn_id, et.ordinal,
                      COALESCE(MIN(m.created_at), t.started_at, t.updated_at) AS occurred_at,
                      t.started_at
               FROM episode_turns AS et
               JOIN turns AS t ON t.id=et.turn_id
               LEFT JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE et.episode_id=?
               GROUP BY et.turn_id, et.ordinal, t.started_at, t.updated_at
               ORDER BY occurred_at, t.started_at, et.turn_id""",
            (episode_id,),
        ).fetchall()
        if all(int(row["ordinal"]) == index for index, row in enumerate(rows, 1)):
            return False
        offset = max(int(row["ordinal"]) for row in rows) + len(rows) + 1
        self._db.execute(
            "UPDATE episode_turns SET ordinal=ordinal+? WHERE episode_id=?",
            (offset, episode_id),
        )
        self._db.executemany(
            """UPDATE episode_turns SET ordinal=?
               WHERE episode_id=? AND turn_id=?""",
            (
                (index, episode_id, str(row["turn_id"]))
                for index, row in enumerate(rows, 1)
            ),
        )
        self._db.execute(
            """UPDATE conversation_episodes
               SET working_summary='', working_summary_claims_json='[]',
                   narrative_summary='', emotional_context_json='{}',
                   outcomes_json='[]', summarized_through_ordinal=0,
                   summary_claimed_at=NULL, summary_retry_at=NULL,
                   summary_failure_count=0, updated_at=?
               WHERE id=?""",
            (now, episode_id),
        )
        log_event(
            logger,
            logging.INFO,
            "episode_turns_reordered",
            stage="storage",
            episode_id=episode_id,
            turns=len(rows),
        )
        return True

    @staticmethod
    def _episode_actions(plan: dict[str, object]) -> list[dict[str, object]]:
        actions = plan.get("episode_actions")
        if isinstance(actions, list):
            return [
                item
                for item in actions
                if isinstance(item, dict) and item.get("action") != "none"
            ]
        bindings = plan.get("episode_bindings")
        return (
            [item for item in bindings if isinstance(item, dict)]
            if isinstance(bindings, list)
            else []
        )

    def _roll_episode(
        self,
        episode_id: str,
        turn_id: str,
        now: float,
        raw_text: str,
        *,
        incoming_turns: int = 1,
    ) -> str:
        turns, raw_tokens = self._episode_size(episode_id)
        if (
            turns + incoming_turns <= 64
            and raw_tokens + estimate_tokens(raw_text) < 64000
        ):
            return episode_id
        row = self._db.execute(
            "SELECT * FROM conversation_episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if row is None:
            return episode_id
        successor = uuid.uuid5(
            uuid.NAMESPACE_URL, f"momoi:episode-successor:{episode_id}:{turn_id}"
        ).hex
        self._db.execute(
            """INSERT OR IGNORE INTO conversation_episodes
               (id, status, title, topics_json, entities_json, open_loops_json,
                salience, created_at, updated_at)
               VALUES (?, 'closing', ?, ?, ?, ?, ?, ?, ?)""",
            (
                successor,
                row["title"],
                row["topics_json"],
                row["entities_json"],
                row["open_loops_json"],
                row["salience"],
                now,
                now,
            ),
        )
        self._db.execute(
            """UPDATE conversation_episodes
               SET status='closed', closed_at=?, updated_at=? WHERE id=?""",
            (now, now, episode_id),
        )
        self._db.execute(
            """INSERT OR IGNORE INTO episode_links
               (from_episode_id, to_episode_id, kind)
               VALUES (?, ?, 'continues')""",
            (successor, episode_id),
        )
        log_event(
            logger,
            logging.INFO,
            "episode_rolled",
            stage="storage",
            episode_id=episode_id,
            successor_episode_id=successor,
            turns=turns,
            raw_tokens=raw_tokens,
        )
        return successor

    def _apply_context_plan_episodes(
        self, turn_id: str, now: float, raw_text: str
    ) -> None:
        row = self._db.execute(
            """SELECT plan_json FROM context_plans
               WHERE turn_id=? AND state<>'superseded'
               ORDER BY revision DESC LIMIT 1""",
            (turn_id,),
        ).fetchone()
        plan = json.loads(str(row["plan_json"])) if row is not None else {}
        actions = self._episode_actions(plan)
        links = plan.get("episode_links", [])
        selected: set[str] = set()
        resolved: dict[str, str] = {}
        self._db.execute("DELETE FROM episode_turns WHERE turn_id=?", (turn_id,))
        for action in actions:
            episode_id = str(action["episode_id"])
            existing = self._db.execute(
                "SELECT * FROM conversation_episodes WHERE id=?", (episode_id,)
            ).fetchone()
            if existing is not None:
                episode_id = self._roll_episode(
                    episode_id, turn_id, now, raw_text
                )
                existing = self._db.execute(
                    "SELECT * FROM conversation_episodes WHERE id=?", (episode_id,)
                ).fetchone()
            resolved[str(action["episode_id"])] = episode_id
            topics = list(action.get("topics") or [])
            entities = list(action.get("entities") or [])
            loops = list(action.get("open_loops") or [])
            status = "open" if loops else "closing"
            if existing is None:
                self._db.execute(
                    """INSERT INTO conversation_episodes
                       (id, status, title, topics_json, entities_json,
                        open_loops_json, salience, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        episode_id,
                        status,
                        str(action["title"]),
                        json.dumps(topics, ensure_ascii=False),
                        json.dumps(entities, ensure_ascii=False),
                        json.dumps(loops, ensure_ascii=False),
                        float(action.get("salience", 0.5)),
                        now,
                        now,
                    ),
                )
            else:
                old_topics = json.loads(str(existing["topics_json"]))
                old_entities = json.loads(str(existing["entities_json"]))
                merged_topics = list(dict.fromkeys([*old_topics, *topics]))[:12]
                merged_entities = list(dict.fromkeys([*old_entities, *entities]))[:20]
                self._db.execute(
                    """UPDATE conversation_episodes
                       SET topics_json=?, entities_json=?, open_loops_json=?,
                           salience=MAX(salience, ?), status=?, closed_at=NULL,
                           updated_at=? WHERE id=?""",
                    (
                        json.dumps(merged_topics, ensure_ascii=False),
                        json.dumps(merged_entities, ensure_ascii=False),
                        json.dumps(loops, ensure_ascii=False),
                        float(action.get("salience", 0.5)),
                        status,
                        now,
                        episode_id,
                    ),
                )
            ordinal = int(
                self._db.execute(
                    """SELECT COALESCE(MAX(ordinal), 0) + 1
                       FROM episode_turns WHERE episode_id=?""",
                    (episode_id,),
                ).fetchone()[0]
            )
            self._db.execute(
                """INSERT INTO episode_turns
                   (episode_id, turn_id, ordinal, relation, unit_ids_json)
                   VALUES (?, ?, ?, 'primary', ?)""",
                (
                    episode_id,
                    turn_id,
                    ordinal,
                    json.dumps(action.get("unit_ids") or [], ensure_ascii=False),
                ),
            )
            selected.add(episode_id)
            self._reindex_episode_terms(episode_id)
        if selected:
            placeholders = ",".join("?" for _ in selected)
            self._db.execute(
                f"""UPDATE conversation_episodes SET status='closed',
                    closed_at=?, updated_at=?
                    WHERE status='closing' AND id NOT IN ({placeholders})
                      AND id IN (
                          SELECT et.episode_id FROM episode_turns AS et
                          JOIN turns AS t ON t.id=et.turn_id WHERE t.kind='owner'
                      )""",
                (now, now, *selected),
            )
        else:
            self._db.execute(
                """UPDATE conversation_episodes SET status='closed',
                   closed_at=?, updated_at=?
                   WHERE status='closing' AND id IN (
                       SELECT et.episode_id FROM episode_turns AS et
                       JOIN turns AS t ON t.id=et.turn_id WHERE t.kind='owner'
                   )""",
                (now, now),
            )
        for link in links if isinstance(links, list) else []:
            if not isinstance(link, dict):
                continue
            source = resolved.get(
                str(link["from_episode_id"]), str(link["from_episode_id"])
            )
            target = resolved.get(
                str(link["to_episode_id"]), str(link["to_episode_id"])
            )
            if source == target:
                continue
            self._db.execute(
                """INSERT OR IGNORE INTO episode_links
                   (from_episode_id, to_episode_id, kind) VALUES (?, ?, ?)""",
                (source, target, str(link["kind"])),
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

    def self_state(self) -> dict[str, object]:
        row = self._db.execute("SELECT * FROM self_state WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("self_state is not initialized")
        return dict(row)

    def self_state_context(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        state = self.self_state()

        def timestamp(value: object) -> str | None:
            return context_timestamp(value) if value is not None else None

        return json.dumps(
            {
                "mood": {
                    "state": state["mood_state"],
                    "intensity": state["mood_intensity"],
                    "cause": state["mood_cause"],
                    "updated_at": timestamp(state["mood_updated_at"]),
                    "age_minutes": max(
                        0, int((now - float(state["mood_updated_at"])) / 60)
                    ),
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
                      pending_reply_last_reason, pending_reply_channel
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
        source_turn = str(row["pending_reply_turn_id"] or "")
        source_messages = [
            {
                "role": str(item["role"]),
                "content": str(item["content"]),
                "delivery_state": str(item["delivery_state"]),
                "timestamp": context_timestamp(item["created_at"]),
            }
            for item in self._db.execute(
                """SELECT role, content, created_at, delivery_state FROM messages
                   WHERE turn_id=? AND (role='user' OR delivery_state IN ('delivered','uncertain'))
                   ORDER BY id""",
                (source_turn,),
            ).fetchall()
        ]
        return {
            "source_turn": source_turn,
            "source_messages": source_messages,
            "expected_response": str(row["pending_reply_expectation"]),
            "waiting_since": context_timestamp(since),
            "waiting_minutes": max(0, int((now - since) / 60)),
            "heartbeat_checks": int(row["pending_reply_checks"] or 0),
            "previous_check_reason": str(row["pending_reply_last_reason"] or ""),
            "check_index": int(row["pending_reply_checks"] or 0) + 1,
            "max_checks": 3,
            "stage_delay_minutes": (1, 3, 6)[
                min(int(row["pending_reply_checks"] or 0), 2)
            ],
            "final_check": int(row["pending_reply_checks"] or 0) >= 2,
            "channel": str(row["pending_reply_channel"] or ""),
            "delivered_followups": int(followups or 0),
        }

    def _apply_mood_update(
        self, update: dict[str, object] | None, now: float
    ) -> None:
        if update is None:
            return
        previous = self._db.execute(
            "SELECT mood_state, mood_intensity FROM self_state WHERE id=1"
        ).fetchone()
        self._db.execute(
            """UPDATE self_state
               SET mood_state=?, mood_intensity=?, mood_cause=?,
                   mood_updated_at=?, updated_at=? WHERE id=1""",
            (
                update["state"],
                update["intensity"],
                str(update["cause"])[:300],
                now,
                now,
            ),
        )
        log_event(
            logger,
            logging.DEBUG,
            "mood_changed",
            previous_state=previous["mood_state"] if previous else "unknown",
            state=update["state"],
            intensity=round(float(update["intensity"]), 2),
            cause=safe_preview(update["cause"], 300),
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
            if row is None or row["heartbeat_claimed_at"] is not None:
                return None
            waiting = bool(str(row["pending_reply_expectation"] or "").strip())
            due: list[tuple[float, str]] = []
            if config.enabled and float(row["next_heartbeat_at"] or 0) > 0:
                due.append((float(row["next_heartbeat_at"]), "ordinary"))
            if waiting and row["pending_reply_next_check_at"] is not None:
                due.append((float(row["pending_reply_next_check_at"]), "reply"))
            if not due:
                return None
            scheduled_at, claim_kind = min(
                due, key=lambda item: (item[0], item[1] != "reply")
            )
            if scheduled_at > now:
                return None
            quiet_end = quiet_until(now, notifications)
            if quiet_end > now:
                column = (
                    "pending_reply_next_check_at"
                    if claim_kind == "reply"
                    else "next_heartbeat_at"
                )
                self._db.execute(
                    f"UPDATE self_state SET {column}=?, updated_at=? WHERE id=1",
                    (quiet_end, now),
                )
                return None
            self._db.execute(
                """UPDATE self_state SET heartbeat_claimed_at=?,
                   heartbeat_claim_kind=? WHERE id=1""",
                (now, claim_kind),
            )
        claimed = dict(row)
        claimed["heartbeat_claim_kind"] = claim_kind
        claimed["heartbeat_scheduled_at"] = scheduled_at
        return claimed

    def claim_manual_heartbeat(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._db:
            row = self._db.execute(
                "SELECT heartbeat_claimed_at FROM self_state WHERE id=1"
            ).fetchone()
            if row is None or row["heartbeat_claimed_at"] is not None:
                return False
            self._db.execute(
                """UPDATE self_state SET heartbeat_claimed_at=?,
                   heartbeat_claim_kind='manual', updated_at=? WHERE id=1""",
                (now, now),
            )
        return True

    def next_heartbeat_due_at(self, enabled: bool) -> float | None:
        row = self._db.execute(
            """SELECT next_heartbeat_at, pending_reply_expectation,
                      pending_reply_next_check_at FROM self_state
               WHERE id=1 AND heartbeat_claimed_at IS NULL"""
        ).fetchone()
        if row is None:
            return None
        waiting = bool(str(row["pending_reply_expectation"] or "").strip())
        due: list[float] = []
        if enabled and float(row["next_heartbeat_at"] or 0) > 0:
            due.append(float(row["next_heartbeat_at"]))
        if waiting and row["pending_reply_next_check_at"] is not None:
            due.append(float(row["pending_reply_next_check_at"]))
        return min(due) if due else None

    def release_heartbeat_claim(self, delay_seconds: float) -> None:
        now = time.time()
        with self._db:
            state = self._db.execute(
                """SELECT heartbeat_claim_kind, pending_reply_expectation
                   FROM self_state WHERE id=1"""
            ).fetchone()
            if (
                state
                and state["heartbeat_claim_kind"] == "reply"
                and str(state["pending_reply_expectation"] or "").strip()
            ):
                self._db.execute(
                    """UPDATE self_state SET heartbeat_claimed_at=NULL,
                       heartbeat_claim_kind=NULL, pending_reply_next_check_at=?,
                       updated_at=? WHERE id=1""",
                    (now + delay_seconds, now),
                )
            elif state and state["heartbeat_claim_kind"] == "reply":
                self._db.execute(
                    """UPDATE self_state SET heartbeat_claimed_at=NULL,
                       heartbeat_claim_kind=NULL, updated_at=? WHERE id=1""",
                    (now,),
                )
            else:
                self._db.execute(
                    """UPDATE self_state SET heartbeat_claimed_at=NULL,
                       heartbeat_claim_kind=NULL, next_heartbeat_at=?, updated_at=?
                       WHERE id=1""",
                    (now + delay_seconds, now),
                )

    def clear_heartbeat_claim(self) -> None:
        with self._db:
            self._db.execute(
                """UPDATE self_state SET heartbeat_claimed_at=NULL,
                   heartbeat_claim_kind=NULL WHERE id=1"""
            )

    def commit_reply_wait(
        self,
        turn_id: str,
        *,
        owner_event_revision: int,
        notification_config: NotificationConfig,
        pending_reply_turn_id: str,
        continue_waiting: bool,
        reason: str,
        mood_update: dict[str, object] | None,
        initial_interval_seconds: float,
        max_interval_seconds: float,
        notification_channel: str = "",
    ) -> int:
        state = self.self_state()
        return self._commit_scheduled_turn(
            turn_id,
            owner_event_revision=owner_event_revision,
            notification_config=notification_config,
            activity=str(state["activity"]),
            result=str(state.get("activity_result") or ""),
            next_heartbeat_at=float(state["next_heartbeat_at"]),
            mood_update=mood_update,
            messages=[],
            reason=reason,
            pending_reply_turn_id=pending_reply_turn_id,
            continue_reply_wait=continue_waiting,
            reply_initial_interval_seconds=initial_interval_seconds,
            reply_max_interval_seconds=max_interval_seconds,
            notification_channel=notification_channel,
            reply_wait_only=True,
        )

    def commit_heartbeat(
        self,
        turn_id: str,
        *,
        owner_event_revision: int,
        notification_config: NotificationConfig,
        activity: str,
        result: str,
        next_heartbeat_at: float,
        mood_update: dict[str, object] | None,
        messages: list[ChannelMessage],
        reason: str,
        reply_expectation: str = "",
        draft: TurnDraft | None = None,
        reply_initial_interval_seconds: float = 60,
        notification_channel: str = "",
    ) -> int:
        return self._commit_scheduled_turn(
            turn_id,
            owner_event_revision=owner_event_revision,
            notification_config=notification_config,
            activity=activity,
            result=result,
            next_heartbeat_at=next_heartbeat_at,
            mood_update=mood_update,
            messages=messages,
            reason=reason,
            reply_expectation=reply_expectation,
            draft=draft,
            reply_initial_interval_seconds=reply_initial_interval_seconds,
            notification_channel=notification_channel,
        )

    def _commit_scheduled_turn(
        self,
        turn_id: str,
        *,
        owner_event_revision: int,
        notification_config: NotificationConfig,
        activity: str,
        result: str,
        next_heartbeat_at: float,
        mood_update: dict[str, object] | None,
        messages: list[ChannelMessage],
        reason: str,
        reply_expectation: str = "",
        draft: TurnDraft | None = None,
        pending_reply_turn_id: str | None = None,
        continue_reply_wait: bool = False,
        reply_initial_interval_seconds: float = 60,
        reply_max_interval_seconds: float | None = None,
        notification_channel: str = "",
        reply_wait_only: bool = False,
    ) -> int:
        now = time.time()
        current = self.self_state()
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
            conversation = self.heartbeat_conversation_snapshot()
            if (
                int(conversation["owner_event_revision"]) != owner_event_revision
                or conversation["owner_busy"]
            ):
                messages = []
            notification_key = (
                "heartbeat.reply_followup"
                if pending_reply_is_current
                else "heartbeat.chat"
            )
            if not self.heartbeat_contact_window(
                notification_key,
                notification_config,
                now,
                apply_cooldown=not pending_reply_is_current,
            )["allowed"]:
                messages = []
            source_json = json.dumps(
                [f"{'reply-wait' if reply_wait_only else 'heartbeat'}:{turn_id}"]
            )
            progress_rows = self._db.execute(
                """SELECT p.text, p.created_at, p.tool_call_id, p.part_index,
                          o.id AS outbox_id, o.state, o.possible_duplicate,
                          o.target_channel
                   FROM turn_progress AS p
                   LEFT JOIN outbox AS o
                     ON o.dedupe_key = 'turn:' || p.turn_id || ':progress:' ||
                        p.tool_call_id || ':' || p.part_index
                   WHERE p.turn_id=?
                   ORDER BY p.created_at, p.tool_call_id, p.part_index""",
                (turn_id,),
            ).fetchall()
            progress_rows = [
                row
                for row in progress_rows
                if row["outbox_id"] is not None
                and str(row["state"] or "") != "superseded"
            ]
            for row in progress_rows:
                if row["outbox_id"] is None:
                    continue
                self._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at, source_event_ids_json,
                        outbox_id, delivery_state)
                       SELECT ?, 'assistant', ?, ?, ?, ?, ?
                       WHERE NOT EXISTS (
                           SELECT 1 FROM messages WHERE outbox_id=?
                       )""",
                    (
                        turn_id,
                        row["text"],
                        row["created_at"],
                        source_json,
                        row["outbox_id"],
                        self._message_delivery_state(
                            str(row["state"]), bool(row["possible_duplicate"])
                        ),
                        row["outbox_id"],
                    ),
                )
            if pending_reply_is_current:
                checks = int(pending["pending_reply_checks"] or 0)
                if continue_reply_wait and checks < 2:
                    delay = reply_initial_interval_seconds * (3, 6)[checks]
                    next_reply_check_at = now + delay
                    self._db.execute(
                        """UPDATE self_state SET
                           pending_reply_checks=pending_reply_checks+1,
                           pending_reply_last_reason=?,
                           pending_reply_next_check_at=? WHERE id=1""",
                        (reason[:500], next_reply_check_at),
                    )
                else:
                    self._cool_active_reply(now, reason)
                    self._db.execute(
                        """UPDATE self_state SET pending_reply_turn_id=NULL,
                           pending_reply_expectation='', pending_reply_since=NULL,
                           pending_reply_checks=0, pending_reply_last_reason='',
                           pending_reply_channel='',
                           pending_reply_next_check_at=NULL
                           WHERE id=1"""
                    )
                    self._supersede_heartbeat_contacts(
                        ("heartbeat.reply_followup",),
                        "reply_waiting_ended",
                        now,
                    )
            if not reply_wait_only:
                self._apply_cooled_reply_action(draft, now)
            self._apply_mood_update(mood_update, now)
            if reply_wait_only:
                self._db.execute(
                    """UPDATE self_state SET heartbeat_claimed_at=NULL,
                       heartbeat_claim_kind=NULL, updated_at=? WHERE id=1""",
                    (now,),
                )
            else:
                self._apply_goal_mutations(draft, now)
                activity_since = (
                    current["activity_since"]
                    if current["activity"] == activity
                    else now
                )
                self._db.execute(
                    """UPDATE self_state SET activity=?, activity_result=?,
                       activity_since=?, last_heartbeat_at=?, next_heartbeat_at=?,
                       heartbeat_claimed_at=NULL, heartbeat_claim_kind=NULL,
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
                heartbeat_record = (
                    "[AUTONOMOUS HEARTBEAT RECORD; not sent to the owner]\n"
                    f"Activity: {activity}\n"
                    f"Result: {result.strip() or '(no concrete result recorded)'}"
                )
                heartbeat_source = json.dumps([f"heartbeat-record:{turn_id}"])
                self._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at, source_event_ids_json,
                        delivery_state)
                       SELECT ?, 'assistant', ?, ?, ?, 'internal'
                       WHERE NOT EXISTS (
                           SELECT 1 FROM messages
                           WHERE turn_id=? AND source_event_ids_json=?
                       )""",
                    (
                        turn_id,
                        heartbeat_record,
                        now,
                        heartbeat_source,
                        turn_id,
                        heartbeat_source,
                    ),
                )
                self._ensure_autonomous_episode(
                    "heartbeat-life",
                    turn_id,
                    "Momoi autonomous life",
                    now,
                    activity,
                    result,
                    *(str(row["text"]) for row in progress_rows),
                )
            target_channel = (
                str(pending["pending_reply_channel"] or "")
                if pending_reply_is_current
                else notification_channel
            )
            if progress_rows and not pending_reply_is_current:
                target_channel = str(
                    progress_rows[-1]["target_channel"] or target_channel
                )
            if progress_rows:
                normalized = [self._outbox_content(message) for message in messages]
                for index, (text, kind, path, payload) in enumerate(normalized):
                    self._db.execute(
                        """INSERT OR IGNORE INTO outbox
                           (turn_id, dedupe_key, text, kind, media_path, payload_json,
                            reply_expectation, target_channel)
                           VALUES (?, ?, ?, ?, ?, ?, '', ?)""",
                        (
                            turn_id,
                            f"turn:{turn_id}:final:{index}",
                            text,
                            kind,
                            path,
                            json.dumps(
                                payload, ensure_ascii=False, separators=(",", ":")
                            ),
                            target_channel,
                        ),
                    )
                    outbox = self._db.execute(
                        "SELECT id FROM outbox WHERE dedupe_key=?",
                        (f"turn:{turn_id}:final:{index}",),
                    ).fetchone()
                    self._db.execute(
                        """INSERT OR IGNORE INTO messages
                           (turn_id, role, content, created_at, source_event_ids_json,
                            outbox_id, delivery_state)
                           VALUES (?, 'assistant', ?, ?, ?, ?, 'queued')""",
                        (
                            turn_id,
                            text,
                            now,
                            source_json,
                            outbox["id"],
                        ),
                    )
                visible = [str(row["text"]) for row in progress_rows] + [
                    text for text, _, _, _ in normalized
                ]
                self._db.execute(
                    """INSERT OR IGNORE INTO notifications
                       (id, turn_id, goal_id, notification_key, priority, reason,
                        messages_json, reply_expectation, state, not_before, created_at,
                        queued_at, target_channel)
                       VALUES (?, ?, 'heartbeat', ?, 'normal', ?, ?, ?,
                               'queued', ?, ?, ?, ?)""",
                    (
                        f"notification:{turn_id}",
                        turn_id,
                        notification_key,
                        reason[:500],
                        json.dumps(visible, ensure_ascii=False),
                        reply_expectation,
                        now,
                        now,
                        now,
                        target_channel,
                    ),
                )
                if reply_expectation:
                    self._bind_turn_reply_expectation(
                        turn_id, reply_expectation, reply_initial_interval_seconds
                    )
            elif messages:
                self._db.execute(
                    """INSERT OR IGNORE INTO notifications
                       (id, turn_id, goal_id, notification_key, priority, reason,
                        messages_json, reply_expectation, state, not_before, created_at,
                        claimed_at, target_channel)
                        VALUES (?, ?, 'heartbeat', ?, 'normal', ?, ?, ?,
                               'pending', ?, ?, ?, ?)""",
                    (
                        f"notification:{turn_id}",
                        turn_id,
                        notification_key,
                        reason[:500],
                        json.dumps(messages, ensure_ascii=False),
                        reply_expectation,
                        now,
                        now,
                        now,
                        target_channel,
                    ),
                )
                notification = self._db.execute(
                    """SELECT * FROM notifications
                       WHERE id=? AND state='pending' AND claimed_at=?""",
                    (f"notification:{turn_id}", now),
                ).fetchone()
                if notification is not None:
                    self._queue_notification_row(notification, now, target_channel)
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   failure_reason=NULL, updated_at=? WHERE id=?""",
                (now, turn_id),
            )
        return len(messages) + len(progress_rows)

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

    def claim_manual_reflection(
        self,
        timezone: str,
        now: float | None = None,
    ) -> dict[str, object] | None:
        now = time.time() if now is None else now
        local_date = datetime.fromtimestamp(now, ZoneInfo(timezone)).date().isoformat()
        reflection_id = f"reflection:{local_date}"
        with self._db:
            self._db.execute(
                """INSERT OR IGNORE INTO reflections
                   (id, local_date, state, scheduled_at, created_at)
                   VALUES (?, ?, 'pending', ?, ?)""",
                (reflection_id, local_date, now, now),
            )
            row = self._db.execute(
                "SELECT * FROM reflections WHERE id=?",
                (reflection_id,),
            ).fetchone()
            if row is None or row["state"] == "running":
                return None
            self._db.execute(
                """UPDATE reflections SET state='running', claimed_at=?,
                   retry_at=NULL, error=NULL WHERE id=?""",
                (now, reflection_id),
            )
            claimed = self._db.execute(
                "SELECT * FROM reflections WHERE id=?",
                (reflection_id,),
            ).fetchone()
        return dict(claimed) if claimed is not None else None

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

    def restore_completed_reflection_claim(self, local_date: str) -> None:
        with self._db:
            self._db.execute(
                """UPDATE reflections SET state='completed', claimed_at=NULL,
                   retry_at=NULL, error=NULL
                   WHERE local_date=? AND state='running'""",
                (local_date,),
            )

    def reflection_source(
        self, local_date: str, timezone: str, token_budget: int
    ) -> dict[str, object]:
        zone = ZoneInfo(timezone)
        start = datetime.fromisoformat(f"{local_date}T00:00:00").replace(tzinfo=zone)
        end = start + timedelta(days=1)
        entries: list[tuple[float, str, str, bool, bool]] = []
        for row in self._db.execute(
            """SELECT role, content, created_at, delivery_state FROM messages
               WHERE created_at>=? AND created_at<?
                 AND (role='user' OR delivery_state IN
                      ('delivered', 'uncertain', 'internal'))
               ORDER BY created_at""",
            (start.timestamp(), end.timestamp()),
        ).fetchall():
            owner = row["role"] == "user"
            label = "OWNER" if owner else "MOMOI"
            if row["delivery_state"] == "internal":
                label = "MOMOI INTERNAL (not sent to owner)"
            elif row["delivery_state"] == "uncertain":
                label = "MOMOI DELIVERY UNCERTAIN"
            entries.append(
                (
                    float(row["created_at"]),
                    label,
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
            f"[{context_timestamp(occurred_at)} {label}]\n{content}"
            for occurred_at, label, content, _, _ in selected
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
        always_memory_actions: list[dict[str, object]] | None = None,
        conversation_actions: list[dict[str, object]] | None = None,
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
            self.apply_always_memory_actions(
                always_memory_actions or [],
                source_id=reflection_id,
                now=now,
            )
            self.apply_conversation_actions(conversation_actions or [], now=now)
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

    def list_reflections(self, limit: int = 90) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        rows = self._db.execute(
            """SELECT * FROM reflections
               ORDER BY local_date DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        results: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            try:
                memories = json.loads(str(item.pop("memories_json", "[]")))
            except (json.JSONDecodeError, TypeError):
                memories = []
            item["memories"] = memories if isinstance(memories, list) else []
            _add_context_timestamps(
                item, ("scheduled_at", "retry_at", "created_at", "completed_at")
            )
            results.append(item)
        return results

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
                       WHERE role='user' OR delivery_state IN
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
            "reminders": int(
                self._db.execute(
                    "SELECT COUNT(*) FROM reminders WHERE status='pending'"
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
                             WHERE t.kind=m.kind AND t.key=m.key
                         )""",
                    (time.time(),),
                ).fetchone()[0]
            ),
        }
        latest_message = self._db.execute(
            """SELECT MAX(created_at) FROM messages
               WHERE role='user' OR delivery_state IN
                   ('delivered', 'uncertain', 'internal')"""
        ).fetchone()[0]
        state = self.self_state()
        return {
            "counts": counts,
            "mood": {
                "state": state["mood_state"],
                "intensity": state["mood_intensity"],
                "cause": state["mood_cause"],
            },
            "activity": {
                "name": state["activity"],
                "result": state.get("activity_result") or "",
                "since": state["activity_since"],
                "since_timestamp": context_timestamp(state["activity_since"]),
            },
            "latest_message_at": latest_message,
            "latest_message_timestamp": (
                context_timestamp(latest_message) if latest_message is not None else None
            ),
        }

    def list_memories(self, limit: int = 200) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        now = time.time()
        rows = self._db.execute(
            """SELECT id, kind, key, content, activation, authority,
                      evidence_quote, importance, created_at, updated_at,
                      expires_at
               FROM memories AS m
               WHERE m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )
               ORDER BY CASE m.activation
                          WHEN 'always' THEN 0
                          WHEN 'recent' THEN 1
                          ELSE 2
                        END,
                        m.updated_at DESC, m.id DESC
               LIMIT ?""",
            (now, limit),
        ).fetchall()
        results: list[dict[str, object]] = []
        for row in rows:
            results.append(self._memory_public_dict(row))
        return results

    def _memory_public_dict(self, row: sqlite3.Row) -> dict[str, object]:
        item = dict(row)
        item["evidence"] = item.pop("evidence_quote")
        _add_context_timestamps(item, ("created_at", "updated_at", "expires_at"))
        return item

    def _active_memory_row(self, memory_id: int) -> sqlite3.Row | None:
        return self._db.execute(
            """SELECT id, kind, key, content, activation, authority,
                      evidence_quote, importance, created_at, updated_at,
                      expires_at
               FROM memories AS m
               WHERE m.id=?
                 AND m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
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
            self._db.execute(
                """INSERT INTO memory_tombstones
                   (kind, key, source_event_id, evidence_quote, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(kind, key) DO UPDATE SET
                     source_event_id=excluded.source_event_id,
                     evidence_quote=excluded.evidence_quote,
                     created_at=excluded.created_at""",
                (
                    row["kind"],
                    row["key"],
                    "dashboard:forget",
                    text,
                    now,
                ),
            )
            self._resolve_memory_conflicts(
                str(row["kind"]), str(row["key"]), "forgotten", now
            )
        return True

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

    def update_goal_owner(
        self,
        goal_id: str,
        *,
        title: str | None = None,
        success_criteria: str | None = None,
        next_action: str | None = None,
        status: str | None = None,
        waiting_for: str | None = None,
        blocked_reason: str | None = None,
    ) -> dict[str, object] | None:
        goal = self.goal(goal_id)
        if goal is None:
            return None
        if goal["status"] in {"done", "cancelled"}:
            raise ValueError("closed goal cannot be updated")
        if title is not None:
            text = title.strip()
            if not text:
                raise ValueError("title must not be empty")
            goal["title"] = text[:500]
        if success_criteria is not None:
            text = success_criteria.strip()
            if not text:
                raise ValueError("success_criteria must not be empty")
            goal["success_criteria"] = text[:2000]
        if next_action is not None:
            goal["next_action"] = next_action.strip()[:2000]
        if waiting_for is not None:
            goal["waiting_for"] = waiting_for.strip()[:2000]
        if blocked_reason is not None:
            goal["blocked_reason"] = blocked_reason.strip()[:2000]
        if status is not None:
            if status not in {"active", "waiting", "blocked"}:
                raise ValueError("status must be active, waiting, or blocked")
            goal["status"] = status
        if goal["status"] == "active" and not goal.get("next_action"):
            raise ValueError("active goal requires next_action")
        if goal["status"] == "waiting" and not goal.get("waiting_for"):
            raise ValueError("waiting goal requires waiting_for")
        if goal["status"] == "blocked" and not goal.get("blocked_reason"):
            raise ValueError("blocked goal requires blocked_reason")
        if goal["status"] == "blocked":
            goal["next_review_at"] = None
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE goals
                   SET title=?, success_criteria=?, status=?, next_action=?,
                       waiting_for=?, blocked_reason=?,
                       next_review_at=?, review_claimed_at=NULL, updated_at=?
                   WHERE id=?""",
                (
                    goal["title"],
                    goal["success_criteria"],
                    goal["status"],
                    goal.get("next_action", ""),
                    goal.get("waiting_for", ""),
                    goal.get("blocked_reason", ""),
                    goal.get("next_review_at"),
                    now,
                    goal_id,
                ),
            )
        return self.goal(goal_id)

    def cancel_goal(self, goal_id: str, reason: str) -> dict[str, object] | None:
        text = reason.strip()
        if not text:
            raise ValueError("reason is required")
        goal = self.goal(goal_id)
        if goal is None:
            return None
        if goal["status"] in {"done", "cancelled"}:
            raise ValueError("closed goal cannot be cancelled")
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE goals
                   SET status='cancelled', latest_result=?, next_review_at=NULL,
                       review_claimed_at=NULL, updated_at=?
                   WHERE id=?""",
                (text[:2000], now, goal_id),
            )
        return self.goal(goal_id)

    def search_goals(self, query: str, max_results: int) -> list[dict[str, object]]:
        if max_results <= 0:
            return []
        query_units = lexical_units(query)
        ranked: list[tuple[float, dict[str, object]]] = []
        for goal in self.list_goals():
            units = lexical_units(
                " ".join(
                    str(goal.get(name) or "")
                    for name in (
                        "id",
                        "title",
                        "success_criteria",
                        "next_action",
                        "waiting_for",
                        "blocked_reason",
                        "latest_result",
                    )
                )
            )
            overlap = len(query_units & units)
            if overlap == 0:
                continue
            score = overlap / max(1, math.sqrt(len(query_units) * len(units)))
            ranked.append((score, goal))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [goal for _, goal in ranked[:max_results]]

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
            lines.append(
                f"- id={goal['id']} status={goal['status']} title={goal['title']} "
                f"next_action={goal['next_action'] or 'none'} "
                f"next_review_at={goal.get('next_review_timestamp') or 'none'} "
                f"retry_at={goal.get('retry_timestamp') or 'none'} "
                f"schedule={json.dumps(goal['schedule'], ensure_ascii=False) if goal['schedule'] else 'none'}"
            )
        return "\n".join(lines)

    def active_reminders_context(self) -> str:
        rows = self._db.execute(
            """SELECT id, text, fire_at, schedule_json FROM reminders
               WHERE status='pending' ORDER BY fire_at LIMIT 20"""
        ).fetchall()
        return "\n".join(
            f"- id={row['id']} fire_at={context_timestamp(row['fire_at'])} "
            f"schedule={row['schedule_json'] or 'none'} text={row['text']}"
            for row in rows
        )

    def list_reminders(
        self, limit: int = 20, *, include_closed: bool = False
    ) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        where = "" if include_closed else "WHERE status='pending'"
        rows = self._db.execute(
            f"""SELECT * FROM reminders {where}
                ORDER BY status='pending' DESC,
                         CASE WHEN status='pending' THEN fire_at END,
                         updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [self._reminder_dict(row) for row in rows]

    def cancel_reminder(self, reminder_id: str) -> dict[str, object] | None:
        now = time.time()
        with self._db:
            cursor = self._db.execute(
                """UPDATE reminders
                   SET status='cancelled', claimed_at=NULL, updated_at=?
                   WHERE id=? AND status='pending'""",
                (now, reminder_id),
            )
        return self.reminder(reminder_id) if cursor.rowcount else None

    def search_reminders(self, query: str, max_results: int) -> list[dict[str, object]]:
        if max_results <= 0:
            return []
        query_units = lexical_units(query)
        ranked: list[tuple[float, dict[str, object]]] = []
        for reminder in self.list_reminders():
            units = lexical_units(f"{reminder['id']} {reminder['text']}")
            overlap = len(query_units & units)
            if overlap == 0:
                continue
            score = overlap / max(1, math.sqrt(len(query_units) * len(units)))
            ranked.append((score, reminder))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [reminder for _, reminder in ranked[:max_results]]

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
                    self._sync_outbox_message(int(message["id"]), "pending")

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
               WHERE media_path=? AND state NOT IN ('sent', 'failed', 'superseded')
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
        _add_context_timestamps(reminder, ("fire_at", "created_at", "updated_at"))
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
            outbox = self._db.execute(
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
            outbox_id = (
                int(outbox.lastrowid)
                if outbox.lastrowid
                else int(
                    self._db.execute(
                        "SELECT id FROM outbox WHERE dedupe_key=?",
                        (reminder_turn_id,),
                    ).fetchone()["id"]
                )
            )
            self._db.execute(
                """INSERT INTO messages
                   (turn_id, role, content, created_at, source_event_ids_json,
                    outbox_id, delivery_state)
                   SELECT ?, 'assistant', ?, ?, ?, ?, 'queued'
                   WHERE NOT EXISTS (
                       SELECT 1 FROM messages WHERE outbox_id=?
                   )""",
                (
                    reminder_turn_id,
                    row["text"],
                    now,
                    json.dumps([f"reminder:{reminder_id}"]),
                    outbox_id,
                    outbox_id,
                ),
            )
            self._ensure_autonomous_episode(
                f"reminder:{reminder_id}",
                reminder_turn_id,
                self._episode_title(str(row["text"]), "Reminder conversation"),
                now,
                row["text"],
            )
        return True

    @staticmethod
    def _goal_dict(row: sqlite3.Row) -> dict[str, object]:
        goal = dict(row)
        _add_context_timestamps(
            goal,
            ("next_review_at", "retry_at", "created_at", "updated_at"),
        )
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
                """UPDATE turns SET stage='message_dispatch', updated_at=?
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
        reply_initial_delay: float = 60,
    ) -> str:
        assistant_messages = reply.messages
        normalized_messages = [
            self._outbox_content(message) for message in assistant_messages
        ]
        turn_id = turn_id or uuid.uuid4().hex
        event_ids = [event.event_id for event in events]
        now = time.time()
        with self._db:
            source_json = json.dumps(event_ids, ensure_ascii=False)
            self._db.execute(
                """INSERT OR IGNORE INTO turns
                   (id, kind, source_ids_json, state, started_at, updated_at)
                   VALUES (?, 'owner', ?, 'running', ?, ?)""",
                (turn_id, source_json, now, now),
            )
            progress = self._db.execute(
                """SELECT text, created_at, tool_call_id, part_index
                   FROM turn_progress
                   WHERE turn_id=? ORDER BY created_at, tool_call_id, part_index""",
                (turn_id,),
            ).fetchall()
            raw_text = "\n".join(
                [
                    user_text,
                    *(str(row["text"]) for row in progress),
                    *(text for text, _kind, _path, _payload in normalized_messages),
                ]
            )
            self._apply_context_plan_episodes(turn_id, now, raw_text)
            self._db.execute(
                """INSERT INTO messages
                   (turn_id, role, content, created_at, source_event_ids_json)
                   VALUES (?, 'user', ?, ?, ?)""",
                (
                    turn_id,
                    user_text,
                    now,
                    source_json,
                ),
            )
            for row in progress:
                outbox = self._db.execute(
                    """SELECT id, state, possible_duplicate FROM outbox
                       WHERE dedupe_key=?""",
                    (
                        f"turn:{turn_id}:progress:{row['tool_call_id']}:"
                        f"{row['part_index']}",
                    ),
                ).fetchone()
                self._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at, source_event_ids_json,
                        outbox_id, delivery_state)
                       VALUES (?, 'assistant', ?, ?, ?, ?, ?)""",
                    (
                        turn_id,
                        row["text"],
                        row["created_at"],
                        source_json,
                        outbox["id"] if outbox else None,
                        self._message_delivery_state(
                            str(outbox["state"]),
                            bool(outbox["possible_duplicate"]),
                        )
                        if outbox
                        else "uncertain",
                    ),
                )
            for index, (assistant_text, kind, path, payload) in enumerate(
                normalized_messages
            ):
                outbox = self._db.execute(
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
                        "",
                        target_channel,
                    ),
                )
                self._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at, source_event_ids_json,
                        outbox_id, delivery_state)
                       VALUES (?, 'assistant', ?, ?, ?, ?, 'queued')""",
                    (
                        turn_id,
                        assistant_text,
                        now,
                        source_json,
                        outbox.lastrowid,
                    ),
                )
            self._index_turn_episode_terms(turn_id)
            self._apply_mood_update(reply.mood_update, now)
            for memory in draft.memories if draft else []:
                self._remember(memory, events, now)
            for conflict in draft.memory_conflicts if draft else []:
                self._propose_memory_conflict(conflict, events, now)
            for forgotten in draft.forgotten_memories if draft else []:
                self._forget_memory(forgotten, events, now)
            self._apply_goal_mutations(draft, now)
            self._apply_reminder_mutations(draft, now)
            self._apply_cooled_reply_action(draft, now)
            self._db.executemany(
                "UPDATE events SET processed=1 WHERE id=?",
                ((event_id,) for event_id in event_ids),
            )
            if reply.expects_reply:
                self._bind_turn_reply_expectation(
                    turn_id, reply.reply_expectation, reply_initial_delay
                )
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   source_ids_json=?, failure_reason=NULL, updated_at=? WHERE id=?""",
                (source_json, now, turn_id),
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
            current = self.goal(goal_id)
            if current is not None:
                goal_record = (
                    "[AUTONOMOUS GOAL REVIEW RECORD; not sent to the owner]\n"
                    f"Goal: {current['title']}\n"
                    f"Status: {current['status']}\n"
                    f"Latest result: {current['latest_result'] or '(none)'}\n"
                    f"Next action: {current['next_action'] or '(none)'}"
                )
                goal_source = json.dumps([f"goal-record:{turn_id}"])
                self._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at, source_event_ids_json,
                        delivery_state)
                       SELECT ?, 'assistant', ?, ?, ?, 'internal'
                       WHERE NOT EXISTS (
                           SELECT 1 FROM messages
                           WHERE turn_id=? AND source_event_ids_json=?
                       )""",
                    (
                        turn_id,
                        goal_record,
                        now,
                        goal_source,
                        turn_id,
                        goal_source,
                    ),
                )
                self._ensure_autonomous_episode(
                    f"goal:{goal_id}",
                    turn_id,
                    str(current["title"]),
                    now,
                    goal_record,
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

    def _notification_key_not_before(
        self,
        priority: str,
        notification_key: str,
        config: NotificationConfig,
        now: float,
        *,
        apply_cooldown: bool = True,
    ) -> float:
        eligible = now
        if priority == "normal":
            eligible = max(eligible, quiet_until(now, config))
            last = self._db.execute(
                """SELECT MAX(n.queued_at) FROM notifications AS n
                   WHERE n.notification_key=? AND (
                       n.state='queued' OR EXISTS (
                           SELECT 1 FROM outbox AS o
                           WHERE o.turn_id=n.turn_id
                             AND (o.state='sent' OR o.possible_duplicate=1)
                       )
                   )""",
                (notification_key,),
            ).fetchone()[0]
            if apply_cooldown and last is not None:
                eligible = max(eligible, float(last) + config.cooldown_seconds)
            if self._db.execute(
                "SELECT 1 FROM events WHERE processed=0 LIMIT 1"
            ).fetchone():
                eligible = max(eligible, now + config.pending_owner_delay_seconds)
        return eligible

    def _notification_not_before(
        self, row: sqlite3.Row, config: NotificationConfig, now: float
    ) -> float:
        return self._notification_key_not_before(
            str(row["priority"]), str(row["notification_key"]), config, now
        )

    def heartbeat_contact_window(
        self,
        notification_key: str,
        config: NotificationConfig,
        now: float | None = None,
        *,
        apply_cooldown: bool = True,
    ) -> dict[str, object]:
        now = time.time() if now is None else now
        eligible_at = self._notification_key_not_before(
            "normal",
            notification_key,
            config,
            now,
            apply_cooldown=apply_cooldown,
        )
        return {"allowed": eligible_at <= now, "eligible_at": eligible_at}

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

    def _queue_notification_row(
        self, row: sqlite3.Row, now: float, primary_channel: str
    ) -> None:
        messages = json.loads(str(row["messages_json"]))
        target_channel = str(row["target_channel"] or primary_channel)
        source = (
            f"heartbeat:{row['turn_id']}"
            if row["goal_id"] == "heartbeat"
            else f"goal:{row['goal_id']}"
        )
        visible_messages: list[str] = []
        for index, message in enumerate(messages):
            visible, kind, path, payload = self._outbox_content(message)
            visible_messages.append(visible)
            dedupe_key = f"notification:{row['id']}:{index}"
            outbox = self._db.execute(
                """INSERT OR IGNORE INTO outbox
                   (turn_id, dedupe_key, text, kind, media_path, payload_json,
                    reply_expectation, target_channel)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["turn_id"],
                    dedupe_key,
                    visible,
                    kind,
                    path,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    row["reply_expectation"] if index == len(messages) - 1 else "",
                    target_channel,
                ),
            )
            outbox_id = (
                int(outbox.lastrowid)
                if outbox.lastrowid
                else int(
                    self._db.execute(
                        "SELECT id FROM outbox WHERE dedupe_key=?", (dedupe_key,)
                    ).fetchone()["id"]
                )
            )
            self._db.execute(
                """INSERT INTO messages
                   (turn_id, role, content, created_at, source_event_ids_json,
                    outbox_id, delivery_state)
                   SELECT ?, 'assistant', ?, ?, ?, ?, 'queued'
                   WHERE NOT EXISTS (
                       SELECT 1 FROM messages WHERE outbox_id=?
                   )""",
                (
                    row["turn_id"],
                    visible,
                    now,
                    json.dumps([source]),
                    outbox_id,
                    outbox_id,
                ),
            )
        if visible_messages:
            episode_key = (
                "heartbeat-life"
                if row["goal_id"] == "heartbeat"
                else f"goal:{row['goal_id']}"
            )
            self._ensure_autonomous_episode(
                episode_key,
                str(row["turn_id"]),
                self._episode_title(visible_messages[0], "Autonomous conversation"),
                now,
                visible_messages,
            )
        self._db.execute(
            """UPDATE notifications SET state='queued', claimed_at=NULL, queued_at=?
               WHERE id=?""",
            (now, row["id"]),
        )

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
            self._queue_notification_row(row, now, primary_channel)
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
