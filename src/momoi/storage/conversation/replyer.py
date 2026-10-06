"""A channel's append-only reply history, rotated in batches."""
import json

from ..core.integrity import decode_stored_json
from ..core.transactions import transaction

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
    def replyer_history_rows(self, channel: str):
        with transaction(self._db):
            stored = self._db.execute(
                "SELECT rows_json FROM replyer_history_windows WHERE channel=?", (channel,)
            ).fetchone()
            previous = decode_stored_json(
                stored["rows_json"], entity="replyer_history_windows", field="rows_json",
                record_id=channel, expected_type=list, fallback=[],
            ) if stored else []
            selected = select_history_rows(self.replyer_dialogue_rows(channel), previous)
            if selected != previous or stored is None:
                self._db.execute(
                    "INSERT INTO replyer_history_windows(channel, rows_json) VALUES (?, ?) "
                    "ON CONFLICT(channel) DO UPDATE SET rows_json=excluded.rows_json",
                    (channel, json.dumps(selected, ensure_ascii=False, separators=(",", ":"))),
                )
            return selected
