import json
import logging
import re
import time

from ..channel import ChannelMessage
from ..config import HeartbeatConfig, NotificationConfig
from ..logging_context import log_event, safe_preview
from ..models import IncomingMessage, TurnDraft
from ..reply_wait import encode_reply_wait
from .scheduling import quiet_until
from .turn_workflow import turn_workflow_kind_sql

logger = logging.getLogger(__name__)
RECENT_HEARTBEAT_LIMIT = 6
_HEARTBEAT_RECORD_ACTIVITY = re.compile(r"^Activity: (.*)$", re.MULTILINE)

def _heartbeat_record_activity(content: str) -> str:
    match = _HEARTBEAT_RECORD_ACTIVITY.search(content)
    return match.group(1).strip()[:300] if match else ""


class HeartbeatStore:
    def self_state(self) -> dict[str, object]:
        row = self._db.execute("SELECT * FROM self_state WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("self_state is not initialized")
        return dict(row)

    def self_state_context(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        state = self.self_state()

        def timestamp(value: object) -> str | None:
            return self.context_timestamp(value) if value is not None else None

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

    def recent_heartbeat_activities(self) -> list[dict[str, str]]:
        workflow = turn_workflow_kind_sql("t")
        rows = self._db.execute(
            f"""SELECT m.content, m.created_at FROM messages AS m
               JOIN turns AS t ON t.id=m.turn_id
               WHERE m.delivery_state='internal'
                 AND {workflow}='heartbeat'
               ORDER BY m.created_at DESC, m.id DESC
               LIMIT ?""",
            (RECENT_HEARTBEAT_LIMIT,),
        ).fetchall()
        items: list[dict[str, str]] = []
        for row in reversed(rows):
            text = _heartbeat_record_activity(str(row["content"] or ""))
            if not text:
                continue
            items.append(
                {
                    "at": self.context_timestamp(row["created_at"]),
                    "text": text,
                }
            )
        return items[-RECENT_HEARTBEAT_LIMIT:]

    def pending_owner_reply(self, now: float | None = None) -> dict[str, object] | None:
        now = time.time() if now is None else now
        row = self._db.execute(
            """SELECT pending_reply_turn_id, pending_reply_expectation,
                      pending_reply_since, pending_reply_last_reason,
                      pending_reply_channel, pending_reply_delay_minutes,
                      pending_reply_next_check_at
               FROM self_state WHERE id=1"""
        ).fetchone()
        if row is None or not str(row["pending_reply_expectation"] or "").strip():
            return None
        since = float(row["pending_reply_since"] or now)
        source_turn = str(row["pending_reply_turn_id"] or "")
        source_messages = [
            {
                "role": str(item["role"]),
                "content": str(item["content"]),
                "delivery_state": str(item["delivery_state"]),
                "timestamp": self.context_timestamp(item["created_at"]),
            }
            for item in self._db.execute(
                """SELECT role, content, created_at, delivery_state FROM messages
                   WHERE turn_id=? AND (role IN ('user', 'event') OR delivery_state IN ('delivered','uncertain'))
                   ORDER BY id""",
                (source_turn,),
            ).fetchall()
        ]
        return {
            "source_turn": source_turn,
            "source_messages": source_messages,
            "expected_information": str(row["pending_reply_expectation"]),
            "reason": str(row["pending_reply_last_reason"] or ""),
            "waiting_since": self.context_timestamp(since),
            "waiting_minutes": max(0, int((now - since) / 60)),
            "delay_minutes": int(row["pending_reply_delay_minutes"] or 0),
            "deadline": self.context_timestamp(row["pending_reply_next_check_at"] or now),
            "channel": str(row["pending_reply_channel"] or ""),
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
            quiet_end = quiet_until(now, self._timezone, notifications)
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
