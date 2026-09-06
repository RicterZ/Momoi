from __future__ import annotations

import json
import time
import uuid

from ..models import AgentReply, IncomingMessage, TurnDraft
from .scheduling import next_schedule_at


def _owner_message_created_at(
    events: list[IncomingMessage], now: float
) -> float:
    times = [
        float(event.occurred_at or event.received_at)
        for event in events
        if event.occurred_at or event.received_at
    ]
    return min(times) if times else now


class TurnCommitStore:
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
        user_created_at = _owner_message_created_at(events, now)
        with self._db:
            source_json = json.dumps(event_ids, ensure_ascii=False)
            self._db.execute(
                """INSERT OR IGNORE INTO turns
                   (id, kind, workflow_kind, source_ids_json, state,
                    started_at, updated_at)
                   VALUES (?, 'owner', 'owner', ?, 'running', ?, ?)""",
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
            self._apply_context_plan_episodes(
                turn_id,
                now,
                raw_text,
                keep_open=reply.should_schedule_reply_wait,
            )
            self._db.execute(
                """INSERT INTO messages
                   (turn_id, role, content, created_at, source_event_ids_json)
                   VALUES (?, 'user', ?, ?, ?)""",
                (
                    turn_id,
                    user_text,
                    user_created_at,
                    source_json,
                ),
            )
            self._archive_progress_messages(turn_id, source_json)
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
            if reply.activity_update is not None:
                current_activity = self._db.execute(
                    "SELECT activity, activity_since FROM self_state WHERE id=1"
                ).fetchone()
                activity_text = str(reply.activity_update["text"])
                activity_since = (
                    current_activity["activity_since"]
                    if current_activity is not None
                    and current_activity["activity"] == activity_text
                    else now
                )
                self._db.execute(
                    """UPDATE self_state SET activity=?, activity_result=?,
                       activity_since=?, updated_at=? WHERE id=1""",
                    (
                        activity_text,
                        str(reply.activity_update["result"])[:2000],
                        activity_since,
                        now,
                    ),
                )
            for memory in draft.memories if draft else []:
                self._remember(memory, events, now)
            for forgotten in draft.forgotten_memories if draft else []:
                self._forget_memory(forgotten, events, now)
            self._apply_goal_mutations(draft, now)
            self._apply_cooled_reply_action(draft, now)
            self._append_turn_journal(
                turn_id,
                "final",
                {
                    "channel": target_channel,
                    "reply_wait": reply.reply_wait,
                    "mood_change": reply.mood_update,
                    **(
                        {"activity_change": reply.activity_update}
                        if reply.activity_update
                        else {}
                    ),
                    "mutations": {
                        "memories": [
                            vars(memory)
                            for memory in (draft.memories if draft else [])
                        ],
                        "forgotten_memories": [
                            vars(memory)
                            for memory in (
                                draft.forgotten_memories if draft else []
                            )
                        ],
                        "goals": list(draft.goals.values()) if draft else [],
                    },
                },
                visibility="internal",
                trust="runtime",
                created_at=now,
            )
            self._db.executemany(
                "UPDATE events SET processed=1 WHERE id=?",
                ((event_id,) for event_id in event_ids),
            )
            if reply.should_schedule_reply_wait:
                self._bind_turn_reply_expectation(
                    turn_id,
                    reply.reply_expectation,
                    reply.reply_wait_delay_minutes,
                    reply.reply_wait_reason,
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
            self._archive_progress_messages(
                turn_id, json.dumps([f"goal:{goal_id}"])
            )
            self._apply_goal_mutations(draft, now)
            if goal_id not in draft.goals:
                current = self.goal(goal_id)
                next_review_at = (
                    next_schedule_at(current["schedule"], self._timezone, now)
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
                archive_day = self._archive_day(now)
                self._ensure_runtime_archive(
                    archive_kind="goal",
                    archive_day=archive_day,
                    episode_key=f"goal:{goal_id}:day:{archive_day}",
                    turn_id=turn_id,
                    title=str(current["title"]),
                    now=now,
                    recall_values=(goal_record,),
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

