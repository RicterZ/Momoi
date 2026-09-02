import json
import sqlite3
import time
import uuid

from ..channel import normalize_channel_message
from ..models import AgentReply, OutboxMessage
from ..reply_wait import decode_reply_wait, encode_reply_wait
from .turn_workflow import turn_workflow_kind_sql


class DeliveryStore:
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

    def _webhook_workflow_id(self, run_id: str) -> str:
        row = self._db.execute(
            "SELECT workflow_id FROM webhook_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError("webhook run not found")
        return str(row["workflow_id"])

    def _heartbeat_turn_time(self, turn_id: str, fallback: float) -> float:
        workflow = turn_workflow_kind_sql("t")
        row = self._db.execute(
            f"""SELECT MIN(m.created_at) AS created_at FROM messages AS m
               JOIN turns AS t ON t.id=m.turn_id
               WHERE m.turn_id=? AND m.delivery_state='internal'
                 AND {workflow}='heartbeat'""",
            (turn_id,),
        ).fetchone()
        return (
            float(row["created_at"])
            if row is not None and row["created_at"] is not None
            else fallback
        )

    def record_webhook_event(
        self,
        run_id: str,
        turn_id: str,
        content: str,
    ) -> int:
        text = str(content or "").strip()
        if not text:
            raise ValueError("webhook event content is empty")
        now = time.time()
        source = json.dumps([turn_id], ensure_ascii=False)
        with self._db:
            existing = self._db.execute(
                """SELECT id FROM messages
                   WHERE turn_id=? AND role='event'
                   ORDER BY id LIMIT 1""",
                (turn_id,),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            inserted = self._db.execute(
                """INSERT INTO messages
                   (turn_id, role, content, created_at, source_event_ids_json,
                    delivery_state)
                   VALUES (?, 'event', ?, ?, ?, 'delivered')""",
                (turn_id, text, now, source),
            )
            workflow_id = self._webhook_workflow_id(run_id)
            archive_day = self._archive_day(now)
            self._ensure_runtime_archive(
                archive_kind="webhook",
                archive_day=archive_day,
                episode_key=f"webhook:{workflow_id}:day:{archive_day}",
                turn_id=turn_id,
                title=f"Webhook {workflow_id}",
                now=now,
                recall_values=(text,),
            )
            return int(inserted.lastrowid)

    def commit_webhook_reply(
        self,
        run_id: str,
        step_index: int,
        turn_id: str,
        reply: AgentReply,
        target_channel: str = "",
    ) -> list[int]:
        step = self.webhook_step(run_id, step_index)
        if step is None:
            raise ValueError("webhook step not found")
        if step["state"] in {"waiting_delivery", "succeeded"}:
            result = step["result"]
            return [int(value) for value in result.get("outbox_ids", [])]  # type: ignore[union-attr]
        if step["state"] != "running":
            raise ValueError("webhook message step is not running")
        normalized = [self._outbox_content(message) for message in reply.messages]
        source_ids = [turn_id]
        source = json.dumps(source_ids, ensure_ascii=False)
        now = time.time()
        with self._db:
            progress = self._db.execute(
                """SELECT text, created_at, tool_call_id, part_index
                   FROM turn_progress
                   WHERE turn_id=? ORDER BY created_at, tool_call_id, part_index""",
                (turn_id,),
            ).fetchall()
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
                        source,
                        outbox["id"] if outbox else None,
                        self._message_delivery_state(
                            str(outbox["state"]),
                            bool(outbox["possible_duplicate"]),
                        )
                        if outbox
                        else "uncertain",
                    ),
                )
            for index, (text, kind, path, payload) in enumerate(normalized):
                outbox = self._db.execute(
                    """INSERT INTO outbox
                       (turn_id, dedupe_key, text, kind, media_path, payload_json,
                        reply_expectation, target_channel)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        turn_id,
                        f"turn:{turn_id}:{index}",
                        text,
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
                    (turn_id, text, now, source, outbox.lastrowid),
                )
            visible = [str(row["text"]) for row in progress] + [
                text for text, _, _, _ in normalized
            ]
            if visible:
                event = self._db.execute(
                    """SELECT created_at FROM messages
                       WHERE turn_id=? AND role='event'
                       ORDER BY id LIMIT 1""",
                    (turn_id,),
                ).fetchone()
                when = float(event["created_at"]) if event is not None else now
                workflow_id = self._webhook_workflow_id(run_id)
                archive_day = self._archive_day(when)
                self._ensure_runtime_archive(
                    archive_kind="webhook",
                    archive_day=archive_day,
                    episode_key=f"webhook:{workflow_id}:day:{archive_day}",
                    turn_id=turn_id,
                    title=f"Webhook {workflow_id}",
                    now=now,
                    recall_values=(visible,),
                )
            self._apply_mood_update(reply.mood_update, now)
            if reply.should_schedule_reply_wait:
                self._bind_turn_reply_expectation(
                    turn_id,
                    reply.reply_expectation,
                    reply.reply_wait_delay_minutes,
                    reply.reply_wait_reason,
                )
            outbox_ids = [
                int(row["id"])
                for row in self._db.execute(
                    "SELECT id FROM outbox WHERE turn_id=? ORDER BY id", (turn_id,)
                ).fetchall()
            ]
            result_json = json.dumps({"outbox_ids": outbox_ids}, separators=(",", ":"))
            step_state = "waiting_delivery" if outbox_ids else "succeeded"
            self._db.execute(
                """UPDATE webhook_steps SET state=?, result_json=?, completed_at=?
                   WHERE run_id=? AND step_index=?""",
                (
                    step_state,
                    result_json,
                    None if outbox_ids else now,
                    run_id,
                    step_index,
                ),
            )
            self._db.execute(
                """UPDATE webhook_runs SET state=?, current_step=?, updated_at=?
                   WHERE id=?""",
                (
                    "waiting_delivery" if outbox_ids else "running",
                    step_index if outbox_ids else step_index + 1,
                    now,
                    run_id,
                ),
            )
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   failure_reason=NULL, updated_at=? WHERE id=?""",
                (now, turn_id),
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
        if len(rows) != len(ids) or states & {"failed", "superseded"}:
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
                result_json = json.dumps(
                    result, ensure_ascii=False, separators=(",", ":")
                )
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
                      o.kind, o.media_path, o.payload_json, o.target_channel
               FROM outbox AS o
               WHERE o.state IN ('pending', 'ambiguous')
                 AND o.next_attempt_at <= ?
                 AND NOT EXISTS (
                     SELECT 1 FROM outbox AS earlier
                     WHERE earlier.id < o.id
                       AND earlier.target_channel = o.target_channel
                       AND earlier.state NOT IN ('sent', 'failed', 'superseded')
                 )
               ORDER BY o.id""",
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
                    channel=str(row["target_channel"] or ""),
                )
            )
        return messages

    def cancel_pending_outbox(self, channel: str, reason: str) -> int:
        with self._db:
            rows = self._db.execute(
                """SELECT id FROM outbox
                   WHERE target_channel=? AND state IN ('pending', 'ambiguous')""",
                (channel,),
            ).fetchall()
            for row in rows:
                outbox_id = int(row["id"])
                self._db.execute(
                    "UPDATE outbox SET state='superseded', last_error=? WHERE id=?",
                    (reason, outbox_id),
                )
                self._sync_outbox_message(outbox_id, "superseded")
        return len(rows)

    def mark_sending(self, outbox_id: int) -> bool:
        with self._db:
            cursor = self._db.execute(
                """UPDATE outbox SET state='sending', attempts=attempts+1
                   WHERE id=? AND state IN ('pending', 'ambiguous')""",
                (outbox_id,),
            )
        return cursor.rowcount == 1

    def mark_not_dispatched(self, outbox_id: int, error: str) -> None:
        with self._db:
            self._db.execute(
                """UPDATE outbox SET state='pending', attempts=MAX(0, attempts-1),
                   next_attempt_at=?, last_error=? WHERE id=?""",
                (time.time() + 2, error, outbox_id),
            )

    def mark_sent(self, outbox_id: int) -> bool:
        activated = False
        with self._db:
            row = self._db.execute(
                """SELECT turn_id, reply_expectation, target_channel
                   FROM outbox WHERE id=?""",
                (outbox_id,),
            ).fetchone()
            self._db.execute(
                "UPDATE outbox SET state='sent', last_error=NULL WHERE id=?",
                (outbox_id,),
            )
            self._sync_outbox_message(outbox_id, "sent")
            decision = decode_reply_wait(row["reply_expectation"]) if row else None
            if decision and row:
                activated = self._activate_reply_expectation(
                    str(row["turn_id"]),
                    decision,
                    str(row["target_channel"] or ""),
                )
                if not activated:
                    self._release_reply_episode_hold(
                        str(row["turn_id"]),
                        time.time(),
                    )
            if row:
                self._finalize_reply_followup_delivery(
                    str(row["turn_id"]),
                    delivered=True,
                )
        return activated

    def _finalize_reply_followup_delivery(
        self,
        execution_turn_id: str,
        *,
        delivered: bool,
    ) -> None:
        workflow = turn_workflow_kind_sql("t")
        followup = self._db.execute(
            f"""SELECT t.state FROM turns AS t
               WHERE t.id=? AND (
                   {workflow}='reply_followup' OR EXISTS (
                       SELECT 1 FROM notifications
                       WHERE turn_id=t.id
                         AND notification_key='heartbeat.reply_followup'
                   )
               )
               UNION ALL
               SELECT 'completed' FROM notifications
               WHERE turn_id=? AND notification_key='heartbeat.reply_followup'
               LIMIT 1""",
            (execution_turn_id, execution_turn_id),
        ).fetchone()
        if followup is None:
            return
        if delivered and str(followup["state"]) == "running":
            return
        state = self._db.execute(
            """SELECT pending_reply_turn_id FROM self_state WHERE id=1"""
        ).fetchone()
        source_turn_id = str(state["pending_reply_turn_id"] or "") if state else ""
        if not delivered:
            self._release_reply_episode_hold(source_turn_id, time.time())
        self._db.execute(
            """UPDATE self_state SET pending_reply_turn_id=NULL,
               pending_reply_expectation='', pending_reply_since=NULL,
               pending_reply_checks=0, pending_reply_last_reason='',
               pending_reply_channel='', pending_reply_delay_minutes=0,
               pending_reply_next_check_at=NULL, updated_at=? WHERE id=1""",
            (time.time(),),
        )

    def _activate_reply_expectation(
        self,
        turn_id: str,
        decision: dict[str, object],
        target_channel: str,
    ) -> bool:
        if self._db.execute(
            "SELECT 1 FROM events WHERE processed=0 LIMIT 1"
        ).fetchone():
            return False
        now = time.time()
        expectation = str(decision["expected_information"])
        reason = str(decision["reason"])
        delay_minutes = int(decision["delay_minutes"])
        due = now + delay_minutes * 60
        self._db.execute(
            """UPDATE self_state SET pending_reply_turn_id=?,
               pending_reply_expectation=?, pending_reply_channel=?,
               pending_reply_since=?, pending_reply_checks=0,
               pending_reply_last_reason=?, pending_reply_delay_minutes=?,
               pending_reply_next_check_at=?, updated_at=? WHERE id=1""",
            (
                turn_id,
                expectation,
                target_channel,
                now,
                reason[:500],
                delay_minutes,
                due,
                now,
            ),
        )
        return True

    def _bind_turn_reply_expectation(
        self,
        turn_id: str,
        expectation: str,
        delay_minutes: int,
        reason: str,
    ) -> bool:
        row = self._db.execute(
            """SELECT id, state, target_channel FROM outbox
               WHERE turn_id=? ORDER BY id DESC LIMIT 1""",
            (turn_id,),
        ).fetchone()
        if row is None:
            raise ValueError("reply expectation requires a visible message")
        encoded = encode_reply_wait(expectation, reason, delay_minutes)
        self._db.execute(
            "UPDATE outbox SET reply_expectation=? WHERE id=?",
            (encoded, row["id"]),
        )
        if row["state"] != "sent":
            return False
        return self._activate_reply_expectation(
            turn_id,
            {
                "expected_information": expectation,
                "reason": reason,
                "delay_minutes": delay_minutes,
            },
            str(row["target_channel"] or ""),
        )

    def mark_ambiguous(self, outbox_id: int, attempts: int, error: str) -> None:
        state = "ambiguous" if attempts < 2 else "failed"
        with self._db:
            row = self._db.execute(
                "SELECT turn_id, reply_expectation FROM outbox WHERE id=?",
                (outbox_id,),
            ).fetchone()
            self._db.execute(
                """UPDATE outbox SET state=?, possible_duplicate=1,
                   next_attempt_at=?, last_error=? WHERE id=?""",
                (state, time.time() + 2, error, outbox_id),
            )
            self._sync_outbox_message(outbox_id, state)
            if (
                state == "failed"
                and row is not None
                and decode_reply_wait(row["reply_expectation"])
            ):
                self._release_reply_episode_hold(
                    str(row["turn_id"]),
                    time.time(),
                )
            if state == "failed" and row is not None:
                self._finalize_reply_followup_delivery(
                    str(row["turn_id"]),
                    delivered=False,
                )

    def mark_failed(self, outbox_id: int, error: str) -> None:
        with self._db:
            row = self._db.execute(
                "SELECT turn_id, reply_expectation FROM outbox WHERE id=?",
                (outbox_id,),
            ).fetchone()
            self._db.execute(
                "UPDATE outbox SET state='failed', last_error=? WHERE id=?",
                (error, outbox_id),
            )
            self._sync_outbox_message(outbox_id, "failed")
            if row is not None and decode_reply_wait(row["reply_expectation"]):
                self._release_reply_episode_hold(
                    str(row["turn_id"]),
                    time.time(),
                )
            if row is not None:
                self._finalize_reply_followup_delivery(
                    str(row["turn_id"]),
                    delivered=False,
                )
