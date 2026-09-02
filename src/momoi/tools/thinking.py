import logging
from typing import Any

from .time_range import parse_history_time_range
from ..observability.events import log_event
from ..models import ToolCall
from ..storage import Store, truncate_tokens

logger = logging.getLogger(__name__)

_DEFAULT_SEARCH_LIMIT = 5
_READ_TOKENS = 1800


class ThinkingTools:
    def __init__(self, store: Store) -> None:
        self.store = store

    def execute(self, call: ToolCall) -> dict[str, Any]:
        try:
            if call.name == "thinking_search":
                return self._search(call.arguments)
            if call.name == "thinking_read":
                return self._read(call.arguments)
            return {
                "ok": False,
                "error": "tool_not_allowed",
                "message": "Thinking tool is not available.",
            }
        except ValueError as error:
            code = str(error)
            return {
                "ok": False,
                "error": code,
                "message": (
                    "time_range is invalid." if code == "invalid_time_range" else code
                ),
            }
        except Exception as error:
            log_event(
                logger,
                logging.ERROR,
                "thinking_tool_failure",
                tool_name=call.name,
                error_type=type(error).__name__,
                exc_info=True,
            )
            return {
                "ok": False,
                "error": "thinking_operation_failed",
                "message": f"Thinking operation failed: {type(error).__name__}.",
            }

    def _search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        turn_id = str(arguments.get("turn_id") or "").strip()
        query = str(arguments.get("query") or "")
        after, before, window = (
            (None, None, {"kind": "turn"})
            if turn_id and arguments.get("time_range") is None
            else parse_history_time_range(arguments.get("time_range"))
        )
        limit = arguments.get("limit", _DEFAULT_SEARCH_LIMIT)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 10
        ):
            limit = _DEFAULT_SEARCH_LIMIT
        cursor = arguments.get("cursor", 0)
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            cursor = 0
        result = self.store.search_thinking(
            turn_id=turn_id,
            query=query,
            after=after,
            before=before,
            stage=str(arguments.get("stage") or "").strip(),
            limit=limit,
            cursor=cursor,
        )
        result["time_range"] = window
        return result

    def _read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.store.read_thinking(
            str(arguments.get("turn_id") or "").strip(),
            str(arguments.get("call_id") or "").strip(),
        )
        if not result.get("ok"):
            return {
                **result,
                "message": "No recorded thinking matched that Turn.",
            }
        calls = []
        for item in result.get("calls") or []:
            if not isinstance(item, dict):
                continue
            rendered = dict(item)
            text = str(rendered.get("reasoning") or "")
            trimmed = truncate_tokens(text, _READ_TOKENS)
            rendered["reasoning"] = trimmed
            rendered["truncated"] = trimmed != text
            calls.append(rendered)
        return {"ok": True, "count": len(calls), "calls": calls}
