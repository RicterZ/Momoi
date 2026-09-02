import time

from ..channel import normalize_channel_message
from ..models import OutboxMessage
from ..reply_wait import decode_reply_wait, encode_reply_wait
from .integrity import decode_stored_json
from .turn_workflow import turn_workflow_kind_sql


class DeliveryStore:
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
            payload = (
                decode_stored_json(
                    raw,
                    entity="outbox",
                    record_id=row["id"],
                    field="payload_json",
                    expected_type=dict,
                    fallback={},
                )
                if raw
                else None
            )
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
