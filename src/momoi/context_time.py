from datetime import datetime


def context_timestamp(value: object) -> str:
    """Render persisted epoch seconds with an unambiguous local date and time."""
    return datetime.fromtimestamp(float(value)).astimezone().isoformat(timespec="seconds")
