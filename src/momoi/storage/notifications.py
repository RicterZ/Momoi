from __future__ import annotations

import json
import sqlite3
import time

from ..config.models import NotificationConfig
from .scheduling import quiet_until


class NotificationStore:
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
            eligible = max(eligible, quiet_until(now, self._timezone, config))
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
            if isinstance(message, dict) and message.get("action") == "voice":
                visible, kind, path, payload = message["text"], "voice", None, {"action": "voice"}
            else:
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
            archive_kind = "heartbeat" if row["goal_id"] == "heartbeat" else "goal"
            archive_time = (
                self._heartbeat_turn_time(str(row["turn_id"]), now)
                if archive_kind == "heartbeat"
                else now
            )
            archive_day = self._archive_day(archive_time)
            title = (
                "心跳"
                if archive_kind == "heartbeat"
                else self._episode_title(
                    visible_messages[0], "Autonomous conversation"
                )
            )
            self._ensure_runtime_archive(
                archive_kind=archive_kind,
                archive_day=archive_day,
                episode_key=(
                    f"heartbeat:day:{archive_day}"
                    if archive_kind == "heartbeat"
                    else f"goal:{row['goal_id']}:day:{archive_day}"
                ),
                turn_id=str(row["turn_id"]),
                title=title,
                now=now,
                recall_values=(visible_messages,),
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
