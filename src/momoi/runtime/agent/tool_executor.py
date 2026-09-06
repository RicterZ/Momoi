import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...contracts import ToolResult
from ...observability.events import log_event
from ...observability.values import compact_log_value, safe_preview
from ...tools.contracts.memory import MEMORY_TOOL_SPECS
from ...tools.contracts.thinking import THINKING_TOOL_SPECS
from ...models import ToolCall, TurnDraft
from ..turn_support import truncate_tool_result_json

logger = logging.getLogger("momoi.runtime.turns")
RESULT_REF_OVERHEAD = 64


@dataclass(frozen=True)
class ToolCallTrace:
    started_at: float
    source: str
    journaled: bool
    turn_id: str
    stage: str
    call_id: str
    round_number: int
    channel: str


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
        self.thinking_tool_names = {
            str(spec["name"]) for spec in THINKING_TOOL_SPECS
        }
        self.artifact_root = artifact_root(config)
        self.result_root = tool_result_root(config)

    def source(self, name: str) -> str:
        if name in {
            "end_turn",
            "send_bubbles",
            "send_voice",
            "tool_enable",
            "read_tool_result",
            "heartbeat_begin",
        }:
            return "runtime"
        if self.mcp.has_tool(name):
            return "mcp"
        if self.builtin_tools.has_tool(name):
            return "builtin"
        if self.agenda_tools.has_tool(name):
            return "agenda"
        if name in self.memory_tool_names:
            return "memory"
        if name in self.thinking_tool_names:
            return "thinking"
        return "unknown"

    def is_external(self, name: str) -> bool:
        return self.mcp.has_tool(name) or self.builtin_tools.has_tool(name)

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

    def begin_trace(
        self,
        call: ToolCall,
        source: str,
        *,
        turn_id: str,
        stage: str,
        call_id: str,
        round_number: int,
        channel: str,
    ) -> ToolCallTrace:
        journaled = source in {
            "mcp",
            "builtin",
            "agenda",
            "memory",
            "thinking",
            "workflow",
        }
        if journaled:
            arguments = dict(call.arguments)
            self.journal(
                turn_id,
                "tool_call",
                {
                    "tool_call_id": call.id,
                    "name": call.name,
                    "source": source,
                    "arguments": compact_log_value(
                        arguments,
                        string_limit=500,
                        item_limit=20,
                    ),
                },
                trust="runtime",
            )
        log_event(
            logger,
            logging.DEBUG,
            "tool_start",
            stage=stage,
            turn_id=turn_id,
            call_id=call_id,
            round=round_number,
            channel=channel,
            tool_call_id=call.id,
            tool_name=call.name,
            arguments=compact_log_value(
                call.arguments,
                string_limit=800,
                item_limit=30,
            ),
        )
        return ToolCallTrace(
            started_at=time.monotonic(),
            source=source,
            journaled=journaled,
            turn_id=turn_id,
            stage=stage,
            call_id=call_id,
            round_number=round_number,
            channel=channel,
        )

    def finish_trace(
        self,
        trace: ToolCallTrace,
        call: ToolCall,
        result: ToolResult,
        draft: TurnDraft,
    ) -> None:
        duration_ms = int((time.monotonic() - trace.started_at) * 1000)
        provenance = result.get("provenance")
        result_message = (
            result.get("message")
            if isinstance(provenance, dict)
            and provenance.get("source") in {"agenda", "memory", "runtime"}
            else None
        )
        compact_result = compact_log_value(
            result,
            string_limit=800,
            item_limit=30,
        )
        log_event(
            logger,
            logging.DEBUG,
            "tool_end",
            stage=trace.stage,
            turn_id=trace.turn_id,
            call_id=trace.call_id,
            round=trace.round_number,
            channel=trace.channel,
            tool_call_id=call.id,
            tool_name=call.name,
            ok=bool(result.get("ok")),
            error=result.get("error"),
            result=compact_result,
            result_message=(
                safe_preview(result_message, 500)
                if result_message is not None
                else None
            ),
            duration_ms=duration_ms,
        )
        draft.tool_calls.append(
            {
                "tool": call.name,
                "ok": bool(result.get("ok")),
                "error": result.get("error"),
                "duration_ms": duration_ms,
            }
        )
        if trace.journaled:
            self.journal(
                trace.turn_id,
                "tool_result",
                {
                    "tool_call_id": call.id,
                    "name": call.name,
                    "ok": bool(result.get("ok")),
                    "error": result.get("error"),
                    "result": compact_result,
                },
                trust=(
                    "untrusted_tool_data"
                    if trace.source in {"mcp", "builtin"}
                    else "runtime"
                ),
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

    async def execute_external(
        self,
        call: ToolCall,
        source: str,
        *,
        turn_id: str,
        allowed_capabilities: set[str] | None,
        artifact_root: Path | None,
    ) -> tuple[ToolResult, bool]:
        capability = (
            self.mcp.capability(call.name)
            if source == "mcp"
            else self.builtin_tools.capability(call)
        )
        if allowed_capabilities is not None and capability not in allowed_capabilities:
            return self.normalize(
                call, {"ok": False, "error": "tool_not_allowed"}, source
            ), False
        if (
            artifact_root is not None
            and call.name in {"read_file", "write_file", "list_dir"}
            and not self.artifact_path_allowed(call, artifact_root)
        ):
            return self.normalize(
                call,
                {"ok": False, "error": "path_outside_autonomous_artifacts"},
                source,
            ), False
        external_effect = capability != "read"
        result = self.store.begin_tool_call(
            turn_id,
            call.id,
            call.name,
            call.arguments,
            capability,
        )
        if result is None:
            result = (
                await self.mcp.call(call.name, call.arguments)
                if source == "mcp"
                else await self.builtin_tools.execute(call)
            )
            result = self.normalize(call, result, source)
            self.store.complete_tool_call(turn_id, call.id, result)
        elif "provenance" not in result:
            result = self.normalize(call, result, source)
        return result, external_effect
