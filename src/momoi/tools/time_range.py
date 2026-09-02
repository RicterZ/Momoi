import time
from datetime import datetime

DEFAULT_HISTORY_LOOKBACK_DAYS = 30


def parse_history_time_range(
    value: object,
) -> tuple[float | None, float | None, dict[str, object]]:
    now = time.time()
    if value is None:
        after = now - DEFAULT_HISTORY_LOOKBACK_DAYS * 86400
        return after, None, {
            "kind": "recent",
            "days": DEFAULT_HISTORY_LOOKBACK_DAYS,
        }
    if not isinstance(value, dict):
        raise ValueError("invalid_time_range")
    kind = value.get("kind")
    if kind == "all" and set(value) == {"kind"}:
        return None, None, {"kind": "all"}
    if kind == "recent" and set(value) <= {"kind", "days"}:
        days = value.get("days", DEFAULT_HISTORY_LOOKBACK_DAYS)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
            raise ValueError("invalid_time_range")
        return now - days * 86400, None, {"kind": "recent", "days": days}
    if kind == "range" and set(value) <= {"kind", "from", "to"}:
        try:
            after = (
                datetime.fromisoformat(str(value["from"])).timestamp()
                if value.get("from")
                else None
            )
            before = (
                datetime.fromisoformat(str(value["to"])).timestamp()
                if value.get("to")
                else None
            )
        except (ValueError, TypeError):
            raise ValueError("invalid_time_range") from None
        if after is None and before is None or (
            after is not None and before is not None and after >= before
        ):
            raise ValueError("invalid_time_range")
        return after, before, {
            "kind": "range",
            **({"from": str(value["from"])} if after is not None else {}),
            **({"to": str(value["to"])} if before is not None else {}),
        }
    raise ValueError("invalid_time_range")
