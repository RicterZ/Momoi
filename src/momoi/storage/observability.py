import time
from datetime import datetime, timedelta

from ..llm.usage import PRICING_NOTE, summarize_usage
from .thinking import month_bounds, parse_month


def _group_thinking_turns(
    calls: list[dict[str, object]],
) -> list[dict[str, object]]:
    buckets: dict[str, list[dict[str, object]]] = {}
    order: list[str] = []
    for call in calls:
        key = str(call.get("turn_id") or "") or f"call:{call.get('call_id') or ''}"
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(call)
    turns: list[dict[str, object]] = []
    for key in order:
        items = sorted(
            buckets[key],
            key=lambda item: (
                float(item.get("created_at") or 0),
                int(item.get("round") or 0),
            ),
        )
        stages: list[str] = []
        tools: list[str] = []
        for item in items:
            stage = str(item.get("stage") or "")
            if stage not in stages:
                stages.append(stage)
            for tool in item.get("tools") or []:
                name = str(tool or "")
                if name and name not in tools:
                    tools.append(name)
        turns.append(
            {
                "id": key,
                "turn_id": str(items[0].get("turn_id") or ""),
                "created_at": items[0].get("created_at"),
                "updated_at": items[-1].get("created_at"),
                "call_count": len(items),
                "stages": stages,
                "tools": tools,
                "excerpt": str(items[0].get("excerpt") or ""),
                "reasoning_chars": sum(
                    int(item.get("reasoning_chars") or 0) for item in items
                ),
            }
        )
    turns.sort(
        key=lambda item: (
            -float(item.get("updated_at") or 0),
            str(item.get("id") or ""),
        )
    )
    return turns


class ObservabilityStore:
    """LLM usage and model-thinking observability."""

    def record_llm_call(
        self,
        *,
        created_at: float,
        turn_id: str = "",
        stage: str = "",
        model: str = "",
        metrics: dict[str, float | int | bool],
    ) -> None:
        with self._db:
            self._db.execute(
                """INSERT INTO llm_usage
                   (created_at, turn_id, stage, model, input_tokens,
                    uncached_tokens, cache_read_tokens, cache_write_tokens,
                    output_tokens, cache_reported)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    created_at,
                    turn_id,
                    stage,
                    model,
                    max(0, int(metrics.get("input") or 0)),
                    max(0, int(metrics.get("uncached") or 0)),
                    max(0, int(metrics.get("cache_read") or 0)),
                    max(0, int(metrics.get("cache_write") or 0)),
                    max(0, int(metrics.get("output") or 0)),
                    1 if metrics.get("cache_reported") else 0,
                ),
            )

    def record_thinking_call(
        self,
        *,
        created_at: float,
        turn_id: str = "",
        call_id: str = "",
        stage: str = "",
        round: int = 0,
        model: str = "",
        tools: list[str] | None = None,
        reasoning: str = "",
    ) -> None:
        self._thinking.record(
            created_at=created_at,
            turn_id=turn_id,
            call_id=call_id,
            stage=stage,
            round=round,
            model=model,
            tools=list(tools or []),
            reasoning=reasoning,
        )

    def search_thinking(
        self,
        *,
        turn_id: str = "",
        query: str = "",
        after: float | None = None,
        before: float | None = None,
        stage: str = "",
        limit: int = 5,
        cursor: int = 0,
    ) -> dict[str, object]:
        hint_at = None
        if turn_id and after is None and before is None:
            row = self._db.execute(
                "SELECT started_at FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            if row is not None:
                hint_at = float(row["started_at"])
        return self._thinking.search(
            turn_id=turn_id,
            query=query,
            after=after,
            before=before,
            stage=stage,
            limit=limit,
            cursor=cursor,
            hint_at=hint_at,
        )

    def read_thinking(self, turn_id: str, call_id: str = "") -> dict[str, object]:
        return self._thinking.read(turn_id, call_id)

    def dashboard_thinking(
        self,
        *,
        month: str = "",
        limit: int = 64,
        cursor: int = 0,
    ) -> dict[str, object]:
        months = self._thinking.available_months()
        selected = str(month or "").strip()
        after: float | None = None
        before: float | None = None
        if selected != "all":
            if not selected:
                selected = datetime.now(self._timezone).strftime("%Y-%m")
            selected = parse_month(selected)
            after, before = month_bounds(selected, self._timezone)
            if selected not in months:
                months = sorted({*months, selected})
        found = self.search_thinking(
            after=after,
            before=before,
            limit=5000,
            cursor=0,
        )
        turns = _group_thinking_turns(found.get("calls") or [])
        start = max(0, cursor)
        size = min(200, max(1, limit))
        page = turns[start : start + size]
        result: dict[str, object] = {
            "ok": True,
            "month": selected,
            "months": months,
            "items": page,
            "count": len(page),
        }
        next_cursor = start + size
        if next_cursor < len(turns):
            result["next_cursor"] = next_cursor
        linked = self.episodes_for_turns(
            [str(item.get("turn_id") or "") for item in page]
        )
        for item in page:
            episode = linked.get(str(item.get("turn_id") or ""))
            if episode:
                item["episode_id"] = episode["episode_id"]
                item["episode_title"] = episode["episode_title"]
        return result

    def dashboard_usage(
        self, *, days: int = 30, now: float | None = None
    ) -> dict[str, object]:
        current = time.time() if now is None else now
        start = datetime.fromtimestamp(current, self._timezone).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=max(1, days) - 1)
        rows = self._db.execute(
            """SELECT created_at, turn_id, stage, model, input_tokens,
                      uncached_tokens, cache_read_tokens, cache_write_tokens,
                      output_tokens, cache_reported
               FROM llm_usage
               WHERE created_at >= ?
               ORDER BY created_at""",
            (start.timestamp(),),
        ).fetchall()
        plugin = self._usage_plugin
        return summarize_usage(
            [dict(row) for row in rows],
            days=days,
            now=current,
            zone=self._timezone,
            estimate=None if plugin is None else plugin.estimate_cost,
            note=PRICING_NOTE,
        )
