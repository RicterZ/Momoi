from datetime import datetime
from zoneinfo import ZoneInfo


def context_timestamp(value: object, timezone: ZoneInfo) -> str:
    """Render persisted epoch seconds with an unambiguous local date and time."""
    return datetime.fromtimestamp(float(value), timezone).isoformat(timespec="seconds")
