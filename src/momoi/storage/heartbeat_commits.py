import json
import time

from ..channel import ChannelMessage
from ..config.models import NotificationConfig
from ..models import IncomingMessage, TurnDraft
from ..reply_wait import encode_reply_wait


class HeartbeatCommitStore:
    def commit_reply_followup(
        self,
        turn_id: str,
        *,
        owner_event_revision: int,
        notification_config: NotificationConfig,
        pending_reply_turn_id: str,
        reason: str,
        mood_update: dict[str, object] | None,
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
            notification_channel=notification_channel,
            reply_followup_only=True,
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
        reply_wait_minutes: int = 0,
        reply_wait_reason: str = "",
        draft: TurnDraft | None = None,
        memory_events: list[IncomingMessage] | None = None,
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
            reply_wait_minutes=reply_wait_minutes,
            reply_wait_reason=reply_wait_reason,
            draft=draft,
            memory_events=memory_events,
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
        reply_wait_minutes: int = 0,
        reply_wait_reason: str = "",
        draft: TurnDraft | None = None,
        memory_events: list[IncomingMessage] | None = None,
        pending_reply_turn_id: str | None = None,
        notification_channel: str = "",
        reply_followup_only: bool = False,
    ) -> int:
        now = time.time()
        current = self.self_state()
        with self._db:
            pending = self._db.execute(
                """SELECT pending_reply_turn_id, pending_reply_channel
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
                [f"{'reply-followup' if reply_followup_only else 'heartbeat'}:{turn_id}"]
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
            message_turn_id = (
                str(pending_reply_turn_id)
                if reply_followup_only and pending_reply_is_current
                else turn_id
            )
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
                        message_turn_id,
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
                delivery_states = {str(row["state"] or "") for row in progress_rows}
                followup_delivered = "sent" in delivery_states
                followup_failed = bool(
                    delivery_states
                    and delivery_states <= {"failed", "superseded"}
                )
                if reply_followup_only and not (
                    followup_delivered or followup_failed
                ):
                    self._db.execute(
                        """UPDATE self_state SET pending_reply_next_check_at=NULL
                           WHERE id=1"""
                    )
                else:
                    self._db.execute(
                        """UPDATE self_state SET pending_reply_turn_id=NULL,
                           pending_reply_expectation='', pending_reply_since=NULL,
                           pending_reply_checks=0, pending_reply_last_reason='',
                           pending_reply_channel='', pending_reply_delay_minutes=0,
                           pending_reply_next_check_at=NULL
                           WHERE id=1"""
                    )
                if reply_followup_only and not followup_failed:
                    self._db.execute(
                        "UPDATE turns SET updated_at=? WHERE id=?",
                        (now, pending_reply_turn_id),
                    )
                    episode_ids = [
                        str(row["episode_id"])
                        for row in self._db.execute(
                            """SELECT episode_id FROM episode_turns
                               WHERE turn_id=?""",
                            (pending_reply_turn_id,),
                        ).fetchall()
                    ]
                    for episode_id in episode_ids:
                        if self._runtime_archive_kind(episode_id):
                            continue
                        self._db.execute(
                            """UPDATE conversation_episodes
                               SET status=CASE
                                     WHEN open_loops_json='[]' THEN 'closing'
                                     ELSE status
                                   END,
                                   closed_at=NULL,
                                   working_summary='',
                                   working_summary_claims_json='[]',
                                   narrative_summary='',
                                   emotional_context_json='{}',
                                   outcomes_json='[]',
                                   summarized_through_ordinal=0,
                                   summary_claimed_at=NULL,
                                   summary_retry_at=NULL,
                                   summary_failure_count=0,
                                   summary_abandoned_at=NULL,
                                   updated_at=?
                               WHERE id=?""",
                            (now, episode_id),
                        )
                        self._reindex_episode_terms(episode_id)
                    self._index_turn_episode_terms(str(pending_reply_turn_id))
            if not reply_followup_only:
                self._apply_cooled_reply_action(draft, now)
            self._apply_mood_update(mood_update, now)
            if reply_followup_only:
                self._db.execute(
                    """UPDATE self_state SET heartbeat_claimed_at=NULL,
                       heartbeat_claim_kind=NULL, updated_at=? WHERE id=1""",
                    (now,),
                )
            else:
                self._apply_goal_mutations(draft, now)
                for memory in draft.memories if draft else []:
                    self._remember(memory, memory_events or [], now)
                for forgotten in draft.forgotten_memories if draft else []:
                    self._forget_memory(forgotten, memory_events or [], now)
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
                        (
                            encode_reply_wait(
                                reply_expectation,
                                reply_wait_reason,
                                reply_wait_minutes,
                            )
                            if reply_expectation
                            else ""
                        ),
                        now,
                        now,
                        now,
                        target_channel,
                    ),
                )
                if reply_expectation:
                    self._bind_turn_reply_expectation(
                        turn_id,
                        reply_expectation,
                        reply_wait_minutes,
                        reply_wait_reason,
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
                        (
                            encode_reply_wait(
                                reply_expectation,
                                reply_wait_reason,
                                reply_wait_minutes,
                            )
                            if reply_expectation
                            else ""
                        ),
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

