"""Request monitoring independent of billable usage and prompt dumps."""
import json
import time
from collections.abc import Callable
from ...memory.storage.transactions import transaction

from ..contracts import MetricsPage, RequestMetricRecord, RequestShape


def compare_shapes(current: RequestShape, previous: RequestShape) -> tuple[int, str]:
    labels = ["tools", "system", "user[0]", "user[1]"]
    prefix = 0
    boundary = "new_tail"
    for index, (a, b) in enumerate(zip(current["parts"], previous["parts"])):
        if a["hash"] != b["hash"]:
            if index == 0 and "tool_parts" in current and "tool_parts" in previous:
                matched = 0
                for left, right in zip(current["tool_parts"], previous["tool_parts"]):
                    if left["hash"] != right["hash"]:
                        break
                    prefix += left["tokens_est"]
                    matched += 1
                return prefix, f"tools[{matched}]"
            boundary = labels[index] if index < len(labels) else f"transcript[{index - 4}]"
            break
        prefix += a["tokens_est"]
    return prefix, boundary


class RequestMetricsRepository:
    def __init__(self, database):
        self._db = database
        self._dashboard_cache: dict[tuple, tuple[float, MetricsPage]] = {}

    def record_first_tool(self, turn_id: str, call_id: str, name: str) -> None:
        """Attach one turn-level latency sample to the request that led to execution."""
        now = time.time()
        with transaction(self._db):
            turn = self._db.execute("SELECT started_at FROM turns WHERE id=?", (turn_id,)).fetchone()
            if turn is None:
                return
            recorded = self._db.execute(
                "SELECT 1 FROM llm_request_metrics WHERE json_extract(data_json, '$.turn_id')=? "
                "AND json_extract(data_json, '$.first_tool_ms') IS NOT NULL LIMIT 1", (turn_id,),
            ).fetchone()
            if recorded:
                return
            row = self._db.execute(
                "SELECT id, data_json FROM llm_request_metrics WHERE status='success' "
                "AND json_extract(data_json, '$.turn_id')=? "
                "AND json_extract(data_json, '$.call_id')=? ORDER BY id DESC LIMIT 1",
                (turn_id, call_id),
            ).fetchone()
            if row is None:
                return
            data = json.loads(row["data_json"])
            data.update(first_tool_ms=max(0, (now - turn["started_at"]) * 1000),
                        first_tool_name=name, first_tool_at=now)
            self._db.execute("UPDATE llm_request_metrics SET data_json=? WHERE id=?",
                             (json.dumps(data), row["id"]))

    def record_request_metric(self, record: RequestMetricRecord) -> None:
        data = dict(record)
        shape = data["shape"]
        # Compare recent compatible requests across stages, not just the previous turn.
        candidates = self._db.execute(
            "SELECT id, data_json FROM llm_request_metrics WHERE route=? ORDER BY id DESC LIMIT 64",
            (data["route"],),
        ).fetchall()
        best = None
        for row in candidates:
            previous = json.loads(row["data_json"])
            prefix, boundary = compare_shapes(shape, previous["shape"])
            if best is None or prefix > best[0]:
                best = prefix, boundary, row["id"], previous.get("stage", ""), previous["shape"]
        data.update(prefix_tokens_est=best[0] if best else None,
                    changed_at=best[1] if best else "no_baseline",
                    compared_request_id=best[2] if best else None,
                    compared_stage=best[3] if best else None)
        if best:
            previous_parts = best[4]["parts"]
            data["system_unchanged"] = shape["parts"][1]["hash"] == previous_parts[1]["hash"]
            common_messages = 0
            for left, right in zip(shape["parts"][2:], previous_parts[2:]):
                if left["hash"] != right["hash"]:
                    break
                common_messages += 1
            data["common_message_prefix_count"] = common_messages
            data["tool_comparison_granularity"] = (
                "item" if "tool_parts" in shape and "tool_parts" in best[4] else "whole"
            )
        data["settings_changed"] = shape["settings_hash"] != best[4]["settings_hash"] if best else None
        fields = shape.get("settings_fields")
        previous_fields = best[4].get("settings_fields") if best else None
        data["changed_settings"] = (
            sorted(k for k in fields.keys() | previous_fields.keys() if fields.get(k) != previous_fields.get(k))
            if fields is not None and previous_fields is not None else None
        )
        usage = data.get("usage") or {}
        input_tokens = usage.get("input")
        hit = usage.get("cache_read") if usage.get("cache_reported") else None
        ratio = hit / input_tokens if hit is not None and input_tokens else None
        reuse = best[0] / shape["input_tokens_est"] if best and shape["input_tokens_est"] else None
        # A diagnostic signal, not a claim about the provider's cache internals.
        data["reuse_ratio_est"] = reuse
        data["cache_alert"] = bool(reuse is not None and reuse >= .8 and ratio is not None
                                   and ratio < .5 and input_tokens >= 4096)
        with transaction(self._db):
            self._db.execute(
                """INSERT INTO llm_request_metrics
                   (created_at, route, stage, model, status, input_tokens, output_tokens,
                    cache_read_tokens, uncached_tokens, duration_ms, first_response_ms,
                    cache_alert, data_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data["created_at"], data["route"], data.get("stage", ""), data["model"],
                 data["status"], input_tokens, usage.get("output"), hit,
                 usage.get("uncached") if hit is not None else None, data["duration_ms"],
                 data["first_response_ms"], int(data["cache_alert"]), json.dumps(data)),
            )
            self._db.execute("DELETE FROM llm_request_metrics WHERE created_at < ?", (time.time() - 30 * 86400,))

    def dashboard_request_metrics(self, *, hours: int = 24, stage: str = "", model: str = "", before: int | None = None, limit: int = 50,
                                  estimate: Callable[..., float] | None = None) -> MetricsPage:
        # The summary runs several full-window aggregates. Pagination changes
        # only the rows, so reuse a recent identical response while the page is
        # open. Bound entries to keep this cache small.
        estimate_key = (
            id(getattr(estimate, "__self__", estimate)),
            id(getattr(estimate, "__func__", None)),
        )
        cache_key = (hours, stage, model, before, limit, estimate_key)
        cached = self._dashboard_cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < 5:
            return cached[1]
        where = "created_at >= ?"
        params = [time.time() - hours * 3600]
        for key, value in (("stage", stage), ("model", model)):
            if value:
                where += f" AND {key}=?"
                params.append(value)
        totals_sql = """COUNT(*) requests, SUM(status='error') errors,
            SUM(status='cancelled') cancelled, SUM(cache_alert) alerts,
            SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens,
            SUM(cache_read_tokens) cache_read_tokens, SUM(uncached_tokens) uncached_tokens,
            SUM(CASE WHEN cache_read_tokens IS NOT NULL THEN input_tokens END) cache_input_tokens,
            COUNT(cache_read_tokens) cache_reported_requests,
            AVG(duration_ms) duration_ms, AVG(json_extract(data_json, '$.first_tool_ms')) first_tool_ms"""
        def enrich(row):
            value = dict(row)
            denominator = value.get("cache_input_tokens")
            value["cache_hit_rate"] = value["cache_read_tokens"] / denominator if denominator else None
            return value
        totals = enrich(self._db.execute(f"SELECT {totals_sql} FROM llm_request_metrics WHERE {where}", params).fetchone())
        stages = [enrich(r) for r in self._db.execute(
            f"SELECT stage, {totals_sql} FROM llm_request_metrics WHERE {where} GROUP BY stage ORDER BY requests DESC", params)]
        trend = [enrich(r) for r in self._db.execute(
            f"SELECT CAST(created_at / 3600 AS INTEGER)*3600 bucket, {totals_sql} FROM llm_request_metrics WHERE {where} GROUP BY bucket ORDER BY bucket", params)]
        choices = self._db.execute("SELECT DISTINCT stage, model FROM llm_request_metrics WHERE created_at>=?", (params[0],)).fetchall()
        if before is not None:
            where += " AND id < ?"
            params.append(before)
        rows = self._db.execute(f"SELECT * FROM llm_request_metrics WHERE {where} ORDER BY id DESC LIMIT ?", [*params, limit + 1]).fetchall()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            detail = json.loads(item.pop("data_json"))
            detail.pop("shape", None)
            item.update(detail)
            usage = item.get("usage")
            item["estimated_cost"] = None
            if estimate is not None and isinstance(usage, dict) and usage.get("input") is not None:
                item["estimated_cost"] = estimate(
                    item["model"], item["created_at"],
                    cache_read=usage.get("cache_read") or 0,
                    uncached=usage.get("uncached") or 0,
                    cache_write=usage.get("cache_write") or 0,
                    output=usage.get("output") or 0,
                )
            items.append(item)
        result = dict(totals=totals, stages=stages, trend=trend, items=items,
                    cost_available=estimate is not None,
                    next_cursor=items[-1]["id"] if len(rows) > limit else None,
                    filters={"stages": sorted({r["stage"] for r in choices}), "models": sorted({r["model"] for r in choices})},
                    retention_days=30, timing="turn_first_tool")
        if len(self._dashboard_cache) >= 16:
            self._dashboard_cache.clear()
        self._dashboard_cache[cache_key] = (now, result)
        return result
