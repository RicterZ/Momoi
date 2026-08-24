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
        raw_times = value.get("times")
        if not isinstance(raw_times, list) or not 1 <= len(raw_times) <= 24:
            raise ValueError("daily schedule requires 1 to 24 times")
        times: list[str] = []
        for item in raw_times:
            if not isinstance(item, str) or not re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d", item
            ):
                raise ValueError("daily schedule times must use HH:MM format")
            times.append(item)
        if len(set(times)) != len(times):
            raise ValueError("daily schedule times must be unique")
        return {"kind": kind, "timezone": timezone, "times": sorted(times)}
    raise ValueError("schedule.kind must be interval or daily")


def next_schedule_at(schedule: dict[str, object], after: float | None = None) -> float:
    normalized = normalize_schedule(schedule)
    after = time.time() if after is None else after
    if normalized["kind"] == "interval":
        return after + int(normalized["every_seconds"])
    zone = ZoneInfo(str(normalized["timezone"]))
    local = datetime.fromtimestamp(after, zone)
    for at in normalized["times"]:
        hour, minute = (int(part) for part in str(at).split(":"))
        candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate.timestamp() > after:
            return candidate.timestamp()
    first = str(normalized["times"][0])
    hour, minute = (int(part) for part in first.split(":"))
    candidate = (local + timedelta(days=1)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
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
