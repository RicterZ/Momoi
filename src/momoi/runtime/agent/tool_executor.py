import json
import logging
from pathlib import Path
from typing import Any

from ...contracts import ToolResult
from ...logging_context import log_event, safe_preview
from ...memory_tools import MEMORY_TOOL_SPECS
from ...models import ToolCall
from ..turn_support import truncate_tool_result_json

logger = logging.getLogger("momoi.runtime.turns")
RESULT_REF_OVERHEAD = 64


def artifact_root(config: Any) -> Path:
    return Path(config.workspace or config.database.parent) / "artifacts"


def tool_result_root(config: Any) -> Path:
    return config.database.parent / "tool-results"


class ToolExecutor:
    """Owns tool classification, durable results, journal, and path policy."""

    def __init__(
        self,
        config: Any,
        store: Any,
        mcp: Any,
        builtin_tools: Any,
        agenda_tools: Any,
        tool_results: Any,
    ) -> None:
        self.config = config
        self.store = store
        self.mcp = mcp
        self.builtin_tools = builtin_tools
        self.agenda_tools = agenda_tools
        self.tool_results = tool_results
        self.memory_tool_names = {str(spec["name"]) for spec in MEMORY_TOOL_SPECS}
        self.artifact_root = artifact_root(config)
        self.result_root = tool_result_root(config)

    def source(self, name: str, *, allow_notify: bool) -> str:
        if name == "send_bubbles" and allow_notify:
            return "agenda"
        if name in {
            "end_turn",
            "send_bubbles",
            "tool_enable",
            "read_tool_result",
            "autonomous_finish",
            "heartbeat_begin",
        }:
            return "runtime"
        if self.mcp.has_tool(name):
            return "mcp"
        if self.builtin_tools.has_tool(name):
            return "builtin"
        if self.agenda_tools.has_tool(name, allow_notify=allow_notify):
            return "agenda"
        if name in self.memory_tool_names:
            return "memory"
        return "unknown"

    def journal(
        self,
        turn_id: str,
        item_type: str,
        payload: dict[str, object],
        *,
        trust: str,
    ) -> None:
        try:
            self.store.append_turn_journal(
                turn_id, item_type, payload, visibility="internal", trust=trust
            )
        except Exception:
            log_event(
                logger,
                logging.WARNING,
                "turn_journal_failed",
                turn_id=turn_id,
                item_type=item_type,
                exc_info=True,
            )

    def normalize(self, call: ToolCall, result: object, source: str) -> ToolResult:
        raw = dict(result) if isinstance(result, dict) else {"value": result}
        ok = raw.get("ok") is True
        error = None if ok else str(raw.get("error") or "tool_failed")
        payload = {
            key: value
            for key, value in raw.items()
            if key not in {"ok", "error", "truncated", "provenance"}
        }
        provenance = {"source": source, "tool": call.name}
        envelope: dict[str, Any] = {
            "ok": ok,
            "error": error,
            "truncated": bool(raw.get("truncated", False)),
            "provenance": provenance,
            **payload,
        }
        serialized = json.dumps(envelope, ensure_ascii=False, default=str)
        result_ref = self.tool_results.save(serialized)
        budget = self.config.tool_result_max_chars - RESULT_REF_OVERHEAD
        if len(serialized) <= budget:
            return {**envelope, "result_ref": result_ref}
        if call.name == "read_file" and ok and isinstance(payload.get("content"), str):
            return {
                **json.loads(truncate_tool_result_json(serialized, budget)),
                "result_ref": result_ref,
            }
        status: dict[str, object] = {"ok": ok, "error": error}
        if raw.get("message") is not None:
            status["message"] = safe_preview(raw["message"], 1000)
        return self.tool_results.read(
            result_ref,
            None,
            max_chars=self.config.tool_result_max_chars,
            provenance=provenance,
            status=status,
        )

    def artifact_path_allowed(self, call: ToolCall, root: Path) -> bool:
        try:
            self.builtin_tools.resolve_path(call.arguments.get("path")).relative_to(
                root.resolve()
            )
            return True
        except (OSError, ValueError):
            return False
