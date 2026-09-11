import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..core.integrity import decode_stored_json


REFLECTION_MEMORY_KINDS = {
    "owner_profile",
    "owner_preference",
    "world_knowledge",
    "self_insight",
    "relationship",
    "shared_experience",
    "practice",
    "tool_skill",
}

def reflection_window(
    local_date: str, at: str, timezone: ZoneInfo, *, end_at: float | None = None,
) -> tuple[float, float]:
    """The configured interval, optionally cut short at a manual trigger (end exclusive)."""
    hour, minute = map(int, at.split(":"))
    start = datetime.fromisoformat(local_date).replace(
        hour=hour, minute=minute, tzinfo=timezone,
    )
    end = (start + timedelta(days=1)).timestamp()
    return start.timestamp(), end if end_at is None else min(end, end_at)

def _reflection_json(
    value: object,
    fallback: list[object] | dict[str, object],
    *,
    record_id: object,
    field: str,
) -> list[object] | dict[str, object]:
    return decode_stored_json(
        value,
        entity="reflection_material",
        record_id=record_id,
        field=field,
        expected_type=type(fallback),
        fallback=fallback,
    )

def _reflection_compact_value(value: object, limit: int = 240) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return " ".join(str(value or "").split())[:limit]
