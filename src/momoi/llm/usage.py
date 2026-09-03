"""Aggregate persisted model usage for dashboard reporting."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Mapping


DEFAULT_MODEL = "deepseek-v4-flash"
CURRENCY = "CNY"
PRICING_NOTE = "只统计接入后的新调用。估算金额由当前用量扩展按官方单价计算。"
TOKEN_ONLY_NOTE = "只统计接入后的新调用。金额统计已关闭。"

EstimateCost = Callable[..., float]


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def local_date(timestamp: float, zone: datetime.tzinfo) -> str:
    return datetime.fromtimestamp(timestamp, zone).date().isoformat()


def _empty_bucket() -> dict[str, float | int]:
    return {
        "requests": 0,
        "input_tokens": 0,
        "uncached_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "cache_reported_input": 0,
        "estimated_cost": 0.0,
    }


def _add_row(
    bucket: dict[str, float | int],
    row: Mapping[str, Any],
    estimate: EstimateCost | None,
) -> None:
    bucket["requests"] = int(bucket["requests"]) + 1
    bucket["input_tokens"] = int(bucket["input_tokens"]) + _as_int(
        row.get("input_tokens")
    )
    bucket["uncached_tokens"] = int(bucket["uncached_tokens"]) + _as_int(
        row.get("uncached_tokens")
    )
    bucket["cache_read_tokens"] = int(bucket["cache_read_tokens"]) + _as_int(
        row.get("cache_read_tokens")
    )
    bucket["cache_write_tokens"] = int(bucket["cache_write_tokens"]) + _as_int(
        row.get("cache_write_tokens")
    )
    bucket["output_tokens"] = int(bucket["output_tokens"]) + _as_int(
        row.get("output_tokens")
    )
    if row.get("cache_reported"):
        bucket["cache_reported_input"] = int(bucket["cache_reported_input"]) + _as_int(
            row.get("input_tokens")
        )
    if estimate is not None:
        bucket["estimated_cost"] = float(bucket["estimated_cost"]) + estimate(
            str(row.get("model") or DEFAULT_MODEL),
            float(row["created_at"]),
            cache_read=_as_int(row.get("cache_read_tokens")),
            uncached=_as_int(row.get("uncached_tokens")),
            cache_write=_as_int(row.get("cache_write_tokens")),
            output=_as_int(row.get("output_tokens")),
        )


def _public_bucket(
    bucket: Mapping[str, float | int], *, cost_available: bool
) -> dict[str, float | int | None]:
    input_tokens = int(bucket["input_tokens"])
    reported = int(bucket["cache_reported_input"])
    cache_read = int(bucket["cache_read_tokens"])
    hit_rate = (cache_read / reported * 100) if reported else 0.0
    return {
        "requests": int(bucket["requests"]),
        "input_tokens": input_tokens,
        "uncached_tokens": int(bucket["uncached_tokens"]),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": int(bucket["cache_write_tokens"]),
        "output_tokens": int(bucket["output_tokens"]),
        "cache_hit_rate": round(hit_rate, 1),
        "estimated_cost": (
            round(float(bucket["estimated_cost"]), 4) if cost_available else None
        ),
    }


def summarize_usage(
    rows: list[Mapping[str, Any]],
    *,
    days: int,
    now: float,
    zone: datetime.tzinfo,
    estimate: EstimateCost | None = None,
    note: str = PRICING_NOTE,
) -> dict[str, Any]:
    cost_available = estimate is not None
    today = datetime.fromtimestamp(now, zone).date()
    start = today - timedelta(days=max(1, days) - 1)
    totals = _empty_bucket()
    today_bucket = _empty_bucket()
    daily: dict[str, dict[str, float | int]] = {}
    models: dict[str, dict[str, float | int]] = {}
    stages: dict[str, dict[str, float | int]] = {}
    for offset in range(max(1, days)):
        daily[(start + timedelta(days=offset)).isoformat()] = _empty_bucket()
    for row in rows:
        created_at = float(row["created_at"])
        day = local_date(created_at, zone)
        if day not in daily:
            continue
        _add_row(totals, row, estimate)
        _add_row(daily[day], row, estimate)
        if day == today.isoformat():
            _add_row(today_bucket, row, estimate)
        model = str(row.get("model") or DEFAULT_MODEL)
        models.setdefault(model, _empty_bucket())
        _add_row(models[model], row, estimate)
        stage = str(row.get("stage") or "unknown")
        stages.setdefault(stage, _empty_bucket())
        _add_row(stages[stage], row, estimate)
    return {
        "source": "local",
        "cost_available": cost_available,
        "currency": CURRENCY,
        "timezone": getattr(zone, "key", None) or str(zone),
        "days": max(1, days),
        "note": note if cost_available else TOKEN_ONLY_NOTE,
        "totals": _public_bucket(totals, cost_available=cost_available),
        "today": _public_bucket(today_bucket, cost_available=cost_available),
        "daily": [
            {
                "date": day,
                **_public_bucket(bucket, cost_available=cost_available),
            }
            for day, bucket in daily.items()
        ],
        "models": [
            {
                "model": name,
                **_public_bucket(bucket, cost_available=cost_available),
            }
            for name, bucket in sorted(
                models.items(), key=lambda item: item[1]["estimated_cost"], reverse=True
            )
        ],
        "stages": [
            {
                "stage": name,
                **_public_bucket(bucket, cost_available=cost_available),
            }
            for name, bucket in sorted(
                stages.items(), key=lambda item: item[1]["estimated_cost"], reverse=True
            )
        ],
    }
