import json
import sqlite3
import time

from ..models import IncomingMessage, TurnDraft
from .episode_sql import runtime_archive_kind_sql
from .integrity import decode_stored_json
from .turn_workflow import turn_workflow_kind_sql


class InboxStore:
    """Owner event ingestion, pending inbox, and interrupted reply state."""

    def owner_channel_revision(self, channel: str) -> int:
        return int(self._db.execute(
            "SELECT COALESCE(MAX(rowid), 0) FROM events WHERE kind=?",
            (f"{channel}.message",),
        ).fetchone()[0])

    def add_event(self, message: IncomingMessage) -> bool:
        payload = {"channel": message.channel, "segments": message.segments}
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
                       pending_reply_channel='', pending_reply_delay_minutes=0,
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

    def _cool_active_reply(self, now: float, _reason: str) -> bool:
        row = self._db.execute(
            """SELECT pending_reply_turn_id, pending_reply_expectation,
                      pending_reply_since, pending_reply_last_reason,
                      pending_reply_delay_minutes, pending_reply_next_check_at
               FROM self_state WHERE id=1"""
        ).fetchone()
        expectation = str(row["pending_reply_expectation"] or "").strip() if row else ""
        if not expectation:
            return False
        self._db.execute(
            """UPDATE self_state SET cooled_reply_expectation=?,
                   cooled_reply_source_turn_id=?, cooled_reply_since=?,
                   cooled_reply_due_at=?, cooled_reply_delay_minutes=?,
                   cooled_reply_waiting_since=?, cooled_reply_review_at=NULL,
                   cooled_reply_checks=0,
                   cooled_reply_reason=?, updated_at=? WHERE id=1""",
            (
                expectation,
                str(row["pending_reply_turn_id"] or ""),
                now,
                row["pending_reply_next_check_at"],
                int(row["pending_reply_delay_minutes"] or 0),
                row["pending_reply_since"],
                str(row["pending_reply_last_reason"] or "")[:500],
                now,
            ),
        )
        self._release_reply_episode_hold(str(row["pending_reply_turn_id"] or ""), now)
        return True

    def _release_reply_episode_hold(self, turn_id: str, now: float) -> None:
        if not turn_id:
            return
        self._db.execute(
            f"""UPDATE conversation_episodes
               SET status='closing', closed_at=NULL, updated_at=?
               WHERE status='open' AND open_loops_json='[]'
                 AND id IN (
                     SELECT episode_id FROM episode_turns WHERE turn_id=?
                 )
                 AND {runtime_archive_kind_sql('conversation_episodes')} IS NULL""",
            (now, turn_id),
        )

    def cooled_reply_expectation_context(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        row = self._db.execute(
            """SELECT cooled_reply_expectation, cooled_reply_source_turn_id,
                      cooled_reply_since, cooled_reply_due_at,
                      cooled_reply_delay_minutes, cooled_reply_waiting_since,
                      cooled_reply_reason
               FROM self_state WHERE id=1"""
        ).fetchone()
        expectation = str(row["cooled_reply_expectation"] or "").strip() if row else ""
        if not expectation:
            return ""
        source_turn = str(row["cooled_reply_source_turn_id"] or "")
        source_rows = self._db.execute(
            """SELECT role, content, created_at, delivery_state FROM messages
               WHERE turn_id=? AND (role IN ('user', 'event') OR delivery_state IN ('delivered','uncertain'))
               ORDER BY id""",
            (source_turn,),
        ).fetchall()
        source_messages = [
            {
                "role": str(item["role"]),
                "content": str(item["content"]),
                "delivery_state": str(item["delivery_state"]),
                "timestamp": self.context_timestamp(item["created_at"]),
            }
            for item in source_rows
        ]
        return json.dumps(
            {
                "state": "owner_replied_before_deadline",
                "expected_information": expectation,
                "reason": str(row["cooled_reply_reason"] or ""),
                "source_turn": source_turn,
                "source_messages": source_messages,
                "waiting_since": self.context_timestamp(
                    row["cooled_reply_waiting_since"] or now
                ),
                "interrupted_at": self.context_timestamp(
                    row["cooled_reply_since"] or now
                ),
                "deadline": self.context_timestamp(row["cooled_reply_due_at"] or now),
                "delay_minutes": int(row["cooled_reply_delay_minutes"] or 0),
                "elapsed_minutes": max(
                    0,
                    int(
                        (
                            float(row["cooled_reply_since"] or now)
                            - float(row["cooled_reply_waiting_since"] or now)
                        )
                        / 60
                    ),
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _apply_cooled_reply_action(
        self, _draft: TurnDraft | None, now: float
    ) -> None:
        self._db.execute(
            """UPDATE self_state SET cooled_reply_expectation='',
               cooled_reply_source_turn_id='', cooled_reply_since=NULL,
               cooled_reply_due_at=NULL, cooled_reply_delay_minutes=0,
               cooled_reply_waiting_since=NULL, cooled_reply_review_at=NULL,
               cooled_reply_checks=0, cooled_reply_reason='', updated_at=?
               WHERE id=1 AND cooled_reply_expectation<>''""",
            (now,),
        )

    def _supersede_heartbeat_contacts(
        self, keys: tuple[str, ...], reason: str, now: float
    ) -> None:
        placeholders = ",".join("?" for _ in keys)
        workflow = turn_workflow_kind_sql("t")
        stale = self._db.execute(
            f"""SELECT o.id, o.state FROM outbox AS o
                LEFT JOIN notifications AS n ON n.turn_id=o.turn_id
                WHERE (
                    n.notification_key IN ({placeholders}) OR EXISTS (
                        SELECT 1 FROM turns AS t
                        WHERE t.id=o.turn_id
                          AND t.kind='autonomous'
                          AND t.state='running'
                          AND {workflow} IN ('heartbeat', 'reply_followup')
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

    def recent_owner_events(self, limit: int = 20) -> list[IncomingMessage]:
        rows = self._db.execute(
            """SELECT * FROM events
               WHERE processed=1 AND content NOT LIKE '/%'
               ORDER BY received_at DESC, rowid DESC LIMIT ?""",
            (max(0, limit),),
        ).fetchall()
        return [self._incoming_message(row) for row in reversed(rows)]

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
        value = (
            decode_stored_json(
                raw,
                entity="event",
                record_id=row["id"],
                field="payload_json",
                expected_type=(dict, list),
                fallback=[],
            )
            if raw
            else []
        )
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
