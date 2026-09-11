from datetime import datetime
from zoneinfo import ZoneInfo


def context_timestamp(value: object, timezone: ZoneInfo) -> str:
    """Render persisted epoch seconds with an unambiguous local date and time."""
    return datetime.fromtimestamp(float(value), timezone).isoformat(timespec="seconds")


def add_context_timestamps(
    value: dict[str, object], fields: tuple[str, ...], timezone: ZoneInfo
) -> None:
    for name in fields:
        if value.get(name) is not None:
            value[f"{name.removesuffix('_at')}_timestamp"] = context_timestamp(
                value[name], timezone
            )
