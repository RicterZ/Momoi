import json
import time

from ...channel import ChannelMessage
from ...config.models import NotificationConfig
from ...models import IncomingMessage, TurnDraft


class HeartbeatCommitStore:
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
        draft: TurnDraft | None = None,
        memory_events: list[IncomingMessage] | None = None,
        notification_channel: str = "",
    ) -> int:
        now = time.time()
        current = self.self_state()
        with self._db:
            conversation = self.heartbeat_conversation_snapshot()
            if (
                int(conversation["owner_event_revision"]) != owner_event_revision
                or conversation["owner_busy"]
            ):
                messages = []
            notification_key = "heartbeat.chat"
            if not self.heartbeat_contact_window(
                notification_config,
                now,
            )["allowed"]:
                messages = []
            source_json = json.dumps([f"heartbeat:{turn_id}"])
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
            self._apply_mood_update(mood_update, now)
            self._apply_goal_mutations(draft, now)
            self._queue_memory_operations(turn_id, draft, memory_events or [], now)
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
            archive_day = self._archive_day(now)
            self._ensure_runtime_archive(
                archive_kind="heartbeat",
                archive_day=archive_day,
                episode_key=f"heartbeat:day:{archive_day}",
                turn_id=turn_id,
                title="心跳",
                now=now,
                recall_values=(
                    activity,
                    result,
                    *(str(row["text"]) for row in progress_rows),
                ),
            )
            target_channel = notification_channel
            if progress_rows:
                target_channel = str(
                    progress_rows[-1]["target_channel"] or target_channel
                )
            if progress_rows:
                normalized = [self._outbox_content(message) for message in messages]
                for index, (text, kind, path, payload) in enumerate(normalized):
                    self._db.execute(
                        """INSERT OR IGNORE INTO outbox
                           (turn_id, dedupe_key, text, kind, media_path, payload_json,
                            target_channel)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
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
                        messages_json, state, not_before, created_at,
                        queued_at, target_channel)
                       VALUES (?, ?, 'heartbeat', ?, 'normal', ?, ?,
                               'queued', ?, ?, ?, ?)""",
                    (
                        f"notification:{turn_id}",
                        turn_id,
                        notification_key,
                        reason[:500],
                        json.dumps(visible, ensure_ascii=False),
                        now,
                        now,
                        now,
                        target_channel,
                    ),
                )
            elif messages:
                self._db.execute(
                    """INSERT OR IGNORE INTO notifications
                       (id, turn_id, goal_id, notification_key, priority, reason,
                        messages_json, state, not_before, created_at,
                        claimed_at, target_channel)
                        VALUES (?, ?, 'heartbeat', ?, 'normal', ?, ?,
                               'pending', ?, ?, ?, ?)""",
                    (
                        f"notification:{turn_id}",
                        turn_id,
                        notification_key,
                        reason[:500],
                        json.dumps(messages, ensure_ascii=False),
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
