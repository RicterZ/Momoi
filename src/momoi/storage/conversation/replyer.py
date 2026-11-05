"""A channel's append-only reply history, rotated in batches."""
import json

from ...memory.text import estimate_tokens
from ..core.integrity import decode_stored_json
from ...memory.storage.transactions import transaction

REPLYER_HISTORY_RETAIN = 12
REPLYER_HISTORY_MAX = 48
REPLYER_HISTORY_SCHEMA = """CREATE TABLE IF NOT EXISTS replyer_history_windows (
    channel TEXT PRIMARY KEY,
    rows_json TEXT NOT NULL
)"""


def select_history_rows(rows, previous):
    """Preserve the existing prefix until the high-water mark is reached."""
    rows = [dict(row) for row in rows if str(row.get("content") or "").strip()]
    if not previous:
        return rows[-REPLYER_HISTORY_RETAIN:]
    current = {row["id"]: row for row in rows}
    # Deleted/edited messages or late delivery must not leave stale evidence in
    # the reply context. They establish a fresh boundary, like an explicit reset.
    if any(current.get(row["id"]) != row for row in previous):
        return rows[-REPLYER_HISTORY_RETAIN:]
    floor, latest = previous[0]["id"], previous[-1]["id"]
    held = {row["id"] for row in previous}
    if any(floor <= row["id"] <= latest and row["id"] not in held for row in rows):
        return rows[-REPLYER_HISTORY_RETAIN:]
    selected = [*previous, *(row for row in rows if row["id"] > latest)]
    if len(selected) >= REPLYER_HISTORY_MAX:
        return selected[-REPLYER_HISTORY_RETAIN:]
    return selected


class ReplyerHistoryStore:
    def _replyer_reset_floor(self, key):
        stored = self._db.execute(
            "SELECT rows_json FROM replyer_history_windows WHERE channel=?", (f"reset:{key}",)
        ).fetchone()
        floor = decode_stored_json(
            stored["rows_json"], entity="replyer_history_windows", field="rows_json",
            record_id=f"reset:{key}", expected_type=int, fallback=0,
        ) if stored else 0
        return floor

    def reset_replyer_history(self, channel: str) -> int:
        """Drop the reply window; messages up to now never re-enter it."""
        keys = ("onebot", "qq_call", "phone_turns") if channel in {"onebot", "qq_call"} else (channel,)
        with transaction(self._db):
            floor = int(self._db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0])
            for key in keys:
                self._db.execute("DELETE FROM replyer_history_windows WHERE channel=?", (key,))
                self._db.execute(
                    "INSERT INTO replyer_history_windows(channel, rows_json) VALUES (?, ?) "
                    "ON CONFLICT(channel) DO UPDATE SET rows_json=excluded.rows_json",
                    (f"reset:{key}", json.dumps(floor)),
                )
        return floor

    def phone_history_rows(self, turn_id):
        key = "phone_turns"
        with transaction(self._db):
            stored = self._db.execute(
                "SELECT rows_json FROM replyer_history_windows WHERE channel=?", (key,)
            ).fetchone()
            previous = decode_stored_json(
                stored["rows_json"], entity="replyer_history_windows", field="rows_json",
                record_id=key, expected_type=list, fallback=[],
            ) if stored else []
            floor = self._replyer_reset_floor(key)
            rows = [row for row in self.replyer_dialogue_rows("qq_call", turn_limit=32)
                    if row["id"] > floor]
            selected = select_phone_turns(rows, previous, exclude_turn=turn_id)
            self._db.execute(
                "INSERT INTO replyer_history_windows(channel, rows_json) VALUES (?, ?) "
                "ON CONFLICT(channel) DO UPDATE SET rows_json=excluded.rows_json",
                (key, json.dumps(selected, ensure_ascii=False)),
            )
            return selected

    def replyer_history_rows(self, channel: str):
        with transaction(self._db):
            stored = self._db.execute(
                "SELECT rows_json FROM replyer_history_windows WHERE channel=?", (channel,)
            ).fetchone()
            previous = decode_stored_json(
                stored["rows_json"], entity="replyer_history_windows", field="rows_json",
                record_id=channel, expected_type=list, fallback=[],
            ) if stored else []
            floor = self._replyer_reset_floor(channel)
            rows = [row for row in self.replyer_dialogue_rows(channel) if row["id"] > floor]
            selected = select_history_rows(rows, previous)
            if selected != previous or stored is None:
                self._db.execute(
                    "INSERT INTO replyer_history_windows(channel, rows_json) VALUES (?, ?) "
                    "ON CONFLICT(channel) DO UPDATE SET rows_json=excluded.rows_json",
                    (channel, json.dumps(selected, ensure_ascii=False, separators=(",", ":"))),
                )
            return selected


def select_phone_turns(rows, previous, *, exclude_turn=""):
    groups = {}
    for row in rows:
        if row["turn_id"] != exclude_turn:
            groups.setdefault(row["turn_id"], []).append(dict(row))
    ids = list(groups)
    previous_ids = list(dict.fromkeys(row["turn_id"] for row in previous))
    if previous_ids and all(identifier in groups for identifier in previous_ids):
        ids = ids[ids.index(previous_ids[0]):]
    else:
        ids = ids[-8:]
    if len(ids) > 16:
        ids = ids[-(8 + (len(ids) - 16) % 8):]
    selected, used = [], 0
    for identifier in reversed(ids):
        group = groups[identifier]
        size = sum(estimate_tokens(str(row["content"])) for row in group)
        if used + size > 6000:
            break
        selected[0:0] = group
        used += size
    return selected
