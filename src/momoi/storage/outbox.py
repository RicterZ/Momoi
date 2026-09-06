import json
import time

from ..channel import (
    ChannelMessage,
    media_path,
    normalize_channel_message,
    render_channel_message,
)
from ..emotions import emotion_slug


class OutboxStore:
    def progress_delivery(self, turn_id: str, tool_call_id: str) -> dict[str, object] | None:
        row = self._db.execute(
            "SELECT text, target_channel, kind, state, last_error FROM outbox WHERE dedupe_key=?",
            (f"turn:{turn_id}:progress:{tool_call_id}:0",),
        ).fetchone()
        return dict(row) if row else None

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

    def _outbox_content(
        self, message: ChannelMessage
    ) -> tuple[str, str, str | None, dict[str, object]]:
        slug = emotion_slug(message) if isinstance(message, str) else None
        if slug is not None:
            asset = self.emotion(slug)
            if asset is None:
                raise ValueError(f"unknown emotion slug: {slug}")
            path = self._stored_asset_path(str(asset["path"]))
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

    def _archive_progress_messages(self, turn_id: str, source_json: str) -> None:
        rows = self._db.execute(
            """SELECT p.text, p.created_at, o.id AS outbox_id,
                      o.state, o.possible_duplicate
               FROM turn_progress AS p
               LEFT JOIN outbox AS o
                 ON o.dedupe_key = 'turn:' || p.turn_id || ':progress:' ||
                    p.tool_call_id || ':' || p.part_index
               WHERE p.turn_id=?
               ORDER BY p.created_at, p.tool_call_id, p.part_index""",
            (turn_id,),
        ).fetchall()
        for row in rows:
            self._db.execute(
                """INSERT INTO messages
                   (turn_id, role, content, created_at, source_event_ids_json,
                    outbox_id, delivery_state)
                   SELECT ?, 'assistant', ?, ?, ?, ?, ?
                   WHERE NOT EXISTS (SELECT 1 FROM messages WHERE outbox_id=?)""",
                (
                    turn_id, row["text"], row["created_at"], source_json,
                    row["outbox_id"],
                    self._message_delivery_state(
                        str(row["state"]), bool(row["possible_duplicate"])
                    ) if row["outbox_id"] is not None else "uncertain",
                    row["outbox_id"],
                ),
            )

    def queue_progress(
        self,
        turn_id: str,
        tool_call_id: str,
        messages: list[ChannelMessage],
        target_channel: str = "",
        *,
        voice: bool = False,
    ) -> None:
        if voice and (len(messages) != 1 or not isinstance(messages[0], str) or not messages[0].strip()):
            raise ValueError("voice progress requires one nonempty text string")
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE turns SET stage='message_dispatch', updated_at=?
                   WHERE id=? AND state='running'""",
                (now, turn_id),
            )
            for index, message in enumerate(messages):
                if voice:
                    # Keep only the original text and delivery mode in SQLite.
                    # The worker synthesizes audio in memory before sending.
                    text, kind, path, payload = message, "voice", None, {"action": "voice"}
                else:
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
