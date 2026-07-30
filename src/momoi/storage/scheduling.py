import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import NotificationConfig


def normalize_schedule(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("schedule must be an object")
    kind = str(value.get("kind") or "")
    timezone = str(value.get("timezone") or "")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("schedule.timezone must be a valid IANA timezone") from None
    if kind == "interval":
        every_seconds = int(value.get("every_seconds", 0))
        if every_seconds < 60:
            raise ValueError("interval schedule requires every_seconds >= 60")
        return {"kind": kind, "timezone": timezone, "every_seconds": every_seconds}
    if kind == "daily":
        at = str(value.get("at") or "")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
            raise ValueError("daily schedule requires at in HH:MM format")
        return {"kind": kind, "timezone": timezone, "at": at}
    raise ValueError("schedule.kind must be interval or daily")


def next_schedule_at(schedule: dict[str, object], after: float | None = None) -> float:
    normalized = normalize_schedule(schedule)
    after = time.time() if after is None else after
    if normalized["kind"] == "interval":
        return after + int(normalized["every_seconds"])
    zone = ZoneInfo(str(normalized["timezone"]))
    hour, minute = (int(part) for part in str(normalized["at"]).split(":"))
    local = datetime.fromtimestamp(after, zone)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= after:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def quiet_until(now: float, config: NotificationConfig) -> float:
    if not config.quiet_start or not config.quiet_end:
        return now
    zone = ZoneInfo(config.timezone)
    local = datetime.fromtimestamp(now, zone)
    start_hour, start_minute = map(int, config.quiet_start.split(":"))
    end_hour, end_minute = map(int, config.quiet_end.split(":"))
    minute = local.hour * 60 + local.minute
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    in_quiet = start <= minute < end if start < end else minute >= start or minute < end
    if not in_quiet:
        return now
    end_local = local.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    if start > end and minute >= start:
        end_local += timedelta(days=1)
    return end_local.timestamp()
