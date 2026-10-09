"""Read-only historical lookup for reflection, without Owner Episode routing."""

import copy

from ...models import ToolCall, TurnDraft
from ...tools.contracts.memory import MEMORY_TOOL_SPECS
from ...tools.time_range import parse_history_time_range
from ...tools.validation import validate_tool_arguments

_MEMORY_SPEC = next(item for item in MEMORY_TOOL_SPECS if item["name"] == "memory_search")
_EPISODE_SPEC = next(item for item in MEMORY_TOOL_SPECS if item["name"] == "episode_search")

REFLECTION_RETRIEVAL_SPECS = [
    {
        "name": "recall",
        "description": (
            "复盘期间只读检索记忆和话题摘要，获取历史背景。使用 query 和可选的 limit，"
            "filters 仅过滤记忆，不限制话题摘要。不使用 Owner 的 units；不会绑定话题、保存上下文计划或修改记忆。"
            "请阅读原始记录核实细节；检索结果只是背景，不能证明事件发生在今日复盘时段。"
        ),
        "input_schema": copy.deepcopy(_MEMORY_SPEC["input_schema"]),
    },
    {
        "name": "conversation_search",
        "description": (
            "搜索复盘时段结束前的原始用户和助手消息。query 按字面匹配，| 分隔候选词；"
            "空 query 浏览时间范围。结果保留说话者、时间和投递状态；内部或不确定消息"
            "不能证明用户已收到。用 next_cursor 继续翻页。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 240},
                "time_range": copy.deepcopy(_EPISODE_SPEC["input_schema"]["properties"]["time_range"]),
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                "cursor": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    *[copy.deepcopy(item) for item in MEMORY_TOOL_SPECS
      if item["name"] in {"memory_search", "episode_search", "episode_read"}],
]


class ReflectionRetrieval:
    def __init__(self, store, memory_tools, period_end):
        self.store = store
        self.memory_tools = memory_tools
        self.period_end = period_end
        self.draft = TurnDraft()

    async def execute(self, call):
        spec = next((s for s in REFLECTION_RETRIEVAL_SPECS if s["name"] == call.name), None)
        if spec is None:
            return {"ok": False, "error": "tool_not_allowed"}
        args, error = validate_tool_arguments(call.name, call.arguments, spec["input_schema"])
        if error:
            return error
        if call.name == "conversation_search":
            return self._conversation_search(args)
        if call.name == "recall":
            results = {}
            for name in ("memory_search", "episode_search"):
                search_args = dict(args)
                if name == "episode_search":
                    search_args.pop("filters", None)
                    search_args["time_range"] = {"kind": "all"}
                results[name] = await self.memory_tools.execute_async(
                    ToolCall(call.id, name, search_args), [], self.draft,
                )
            return {"ok": all(result.get("ok") for result in results.values()),
                    "historical_background": True, "results": results}
        return await self.memory_tools.execute_async(
            ToolCall(call.id, call.name, args), [], self.draft,
        )

    def _conversation_search(self, args):
        try:
            after, before, _ = parse_history_time_range(args.get("time_range", {"kind": "all"}))
        except ValueError:
            return {"ok": False, "error": "invalid_time_range"}
        clauses = ["role IN ('user', 'assistant')", "created_at < ?"]
        values = [min(before, self.period_end) if before is not None else self.period_end]
        if after is not None:
            clauses.append("created_at >= ?")
            values.append(after)
        terms = [term.strip() for term in args["query"].split("|") if term.strip()]
        if terms:
            clauses.append("(" + " OR ".join("instr(lower(content), lower(?)) > 0" for _ in terms) + ")")
            values.extend(terms)
        limit, cursor = args.get("limit", 5), args.get("cursor", 0)
        rows = self.store._db.execute(
            "SELECT id, turn_id, role, content, created_at, delivery_state FROM messages WHERE "
            + " AND ".join(clauses) + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (*values, limit + 1, cursor),
        ).fetchall()
        messages = []
        for row in rows[:limit]:
            item = dict(row)
            item["truncated"] = len(item["content"]) > 4000
            item["content"] = item["content"][:4000]
            item["time"] = self.store.context_timestamp(item["created_at"])
            messages.append(item)
        return {"ok": True, "messages": messages,
                "next_cursor": cursor + limit if len(rows) > limit else None}
