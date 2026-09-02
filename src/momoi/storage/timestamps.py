from zoneinfo import ZoneInfo

from ..context_time import context_timestamp


def add_context_timestamps(
    value: dict[str, object], fields: tuple[str, ...], timezone: ZoneInfo
) -> None:
    for name in fields:
        if value.get(name) is not None:
            value[f"{name.removesuffix('_at')}_timestamp"] = context_timestamp(
                value[name], timezone
            )
