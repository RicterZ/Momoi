import json
import time

from ...channel import normalize_channel_message
from ...models import OutboxMessage
from ..core.integrity import decode_stored_json


class DeliveryStore:
    def outbox_dispatchable(self, outbox_id: int) -> bool:
        return self._db.execute(
            "SELECT 1 FROM outbox WHERE id=? AND state IN ('pending', 'ambiguous')",
            (outbox_id,),
        ).fetchone() is not None

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

    def cancel_pending_outbox(self, channel: str, reason: str, *, source_message_id: str | None = None) -> int:
        with self._db:
            rows = self._db.execute(
                """SELECT id FROM outbox
                   WHERE target_channel=? AND state IN ('pending', 'ambiguous')
                     AND (? IS NULL OR EXISTS (
                         SELECT 1 FROM turns t, json_each(t.source_ids_json) src
                         JOIN events e ON e.id=src.value
                         WHERE t.id=outbox.turn_id AND e.kind=? AND e.message_id=?
                     ))""",
                (channel, source_message_id, f"{channel}.message", source_message_id),
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

    def record_delivery_receipt(self, outbox_id: int, message_id: str) -> None:
        row = self._db.execute("SELECT payload_json FROM outbox WHERE id=?", (outbox_id,)).fetchone()
        if row is None or not message_id:
            return
        payload = json.loads(row[0] or "{}")
        payload["_delivery_receipt"] = {"message_id": str(message_id)}
        with self._db:
            self._db.execute("UPDATE outbox SET payload_json=? WHERE id=?",
                             (json.dumps(payload, ensure_ascii=False), outbox_id))

    def recallable_delivery(self, channel: str, outbox_id: int | None = None):
        row = self._db.execute(
            """SELECT id, payload_json FROM outbox
               WHERE target_channel=? AND state='sent' AND (? IS NULL OR id=?)
                 AND json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE 'null' END, '$._delivery_receipt.message_id') IS NOT NULL
               ORDER BY id DESC LIMIT 1""", (channel, outbox_id, outbox_id),
        ).fetchone()
        if row is None:
            raise ValueError("没有可撤回的已发送消息")
        return int(row['id']), str(json.loads(row['payload_json'])['_delivery_receipt']['message_id'])

    def mark_ambiguous(self, outbox_id: int, attempts: int, error: str) -> None:
        state = "ambiguous" if attempts < 2 else "failed"
        with self._db:
            self._db.execute(
                """UPDATE outbox SET state=?, possible_duplicate=1,
                   next_attempt_at=?, last_error=? WHERE id=?""",
                (state, time.time() + 2, error, outbox_id),
            )
            self._sync_outbox_message(outbox_id, state)

    def mark_failed(self, outbox_id: int, error: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE outbox SET state='failed', last_error=? WHERE id=?",
                (error, outbox_id),
            )
            self._sync_outbox_message(outbox_id, "failed")

    def mark_sent(self, outbox_id: int) -> None:
        with self._db:
            self._db.execute(
                "UPDATE outbox SET state='sent', last_error=NULL WHERE id=?",
                (outbox_id,),
            )
            self._sync_outbox_message(outbox_id, "sent")
