import json
import logging
import os
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Iterator, Mapping


_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("momoi_log_context", default={})
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:/@+-]+$")
_SENSITIVE_KEY = re.compile(
    r"^(?:api[_-]?key|authorization|cookie|credentials?|password|secret|"
    r"access[_-]?token|refresh[_-]?token)$",
    re.IGNORECASE,
)
_PREFERRED_FIELDS = (
    "stage",
    "turn_id",
    "call_id",
    "round",
    "attempt",
    "attempt_max",
    "channel",
    "event_id",
    "goal_id",
    "run_id",
    "workflow_id",
    "step_index",
    "outbox_id",
    "tool_call_id",
    "tool_name",
    "server",
    "model",
    "duration_ms",
    "ok",
    "error_type",
    "reason",
)
TRACE = 5
logging.addLevelName(TRACE, "TRACE")
logging.TRACE = TRACE
_LEVEL_COLORS = {
    TRACE: "\033[2;37m",
    logging.DEBUG: "\033[36m",
    logging.INFO: "\033[32m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[1;31m",
}
_COLOR_RESET = "\033[0m"
_EVENT_GROUPS = {
    "service_start": "SERVICE",
    "channel_start": "SERVICE",
    "channel_connected": "SERVICE",
    "owner_message_received": "OWNER",
    "owner_command_accepted": "OWNER",
    "owner_command_ignored": "OWNER",
    "context_plan_complete": "PLAN",
    "context_plan_invalid": "PLAN",
    "context_plan_degraded": "PLAN",
    "context_plan_normalized": "PLAN",
    "context_recall": "RECALL",
    "heartbeat_plan_complete": "PLAN",
    "heartbeat_plan_invalid": "PLAN",
    "heartbeat_plan_degraded": "PLAN",
    "llm_response": "MODEL",
    "llm_retry": "MODEL",
    "llm_failure": "MODEL",
    "tool_start": "TOOL",
    "tool_end": "TOOL",
    "turn_complete": "TURN",
    "turn_failure": "TURN",
    "outbox_retry": "DELIVERY",
    "outbox_ambiguous": "DELIVERY",
    "outbox_failure": "DELIVERY",
    "channel_disconnected": "CHANNEL",
    "channel_frame_invalid": "CHANNEL",
    "channel_inbound_failure": "CHANNEL",
    "channel_media_failure": "CHANNEL",
    "channel_message_dropped": "CHANNEL",
    "channel_notify_failure": "CHANNEL",
    "channel_poll_failure": "CHANNEL",
    "channel_poll_rejected": "CHANNEL",
    "channel_reference_failure": "CHANNEL",
    "channel_session_stale": "CHANNEL",
    "channel_stop": "CHANNEL",
    "mcp_call_end": "MCP",
    "mcp_call_failure": "MCP",
    "mcp_cleanup_failure": "MCP",
    "mcp_connect_failure": "MCP",
    "mcp_connected": "MCP",
    "mcp_connection_interrupted": "MCP",
    "mcp_reconnect_failure": "MCP",
    "mcp_worker_failure": "MCP",
    "webhook_api_started": "WEBHOOK",
    "webhook_delivery_end": "WEBHOOK",
    "webhook_run_accepted": "WEBHOOK",
    "webhook_run_complete": "WEBHOOK",
    "webhook_run_failure": "WEBHOOK",
    "webhook_step_end": "WEBHOOK",
    "webhook_step_start": "WEBHOOK",
    "workflow_catalog_empty": "WORKFLOW",
    "workflow_catalog_loaded": "WORKFLOW",
    "workflow_executor_skipped": "WORKFLOW",
    "workflow_loaded": "WORKFLOW",
    "workflow_skipped": "WORKFLOW",
    "agenda_tool_failure": "AGENDA",
    "memory_tool_failure": "MEMORY",
    "thinking_record_failed": "THINKING",
    "thinking_tool_failure": "THINKING",
    "dashboard_start": "SERVICE",
    "mood_changed": "TURN",
    "notification_queued": "DELIVERY",
    "owner_updates_injected": "OWNER",
    "goal_deferred": "AGENDA",
    "goal_queued": "AGENDA",
    "heartbeat_deferred": "AGENDA",
    "heartbeat_queued": "AGENDA",
    "workspace_prompt_loaded": "PROMPT",
    "workspace_prompt_missing": "PROMPT",
    "mcp_config_loaded": "MCP",
    "mcp_config_missing": "MCP",
    "usage_plugin_loaded": "SERVICE",
    "reflection_queued": "AGENDA",
    "reminder_fired": "AGENDA",
    "prompt_reload_failed": "SERVICE",
    "recall_index_compaction_complete": "STORAGE",
    "recall_index_compaction_failure": "STORAGE",
    "recall_index_compaction_start": "STORAGE",
    "recall_index_migration_complete": "STORAGE",
    "recall_index_migration_failure": "STORAGE",
    "recall_index_migration_start": "STORAGE",
}
def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def current_log_context() -> dict[str, Any]:
    return dict(_CONTEXT.get())


@contextmanager
def log_context(**fields: Any) -> Iterator[dict[str, Any]]:
    merged = current_log_context()
    merged.update({key: value for key, value in fields.items() if value is not None})
    token = _CONTEXT.set(merged)
    try:
        yield merged
    finally:
        _CONTEXT.reset(token)


@contextmanager
def captured_log_context(snapshot: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    token = _CONTEXT.set(dict(snapshot))
    try:
        yield current_log_context()
    finally:
        _CONTEXT.reset(token)


def _redact(value: Any, key: str = "") -> Any:
    if key and _SENSITIVE_KEY.search(key):
        return "[redacted]"
    if isinstance(value, Mapping):
        if value.get("type") == "base64" and isinstance(value.get("data"), str):
            return {
                str(item_key): (
                    f"[omitted {len(str(item_value))} base64 chars]"
                    if item_key == "data"
                    else _redact(item_value, str(item_key))
                )
                for item_key, item_value in value.items()
            }
        return {
            str(item_key): _redact(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str) and value.startswith("data:") and ";base64," in value:
        prefix, encoded = value.split(",", 1)
        return f"{prefix},[omitted {len(encoded)} base64 chars]"
    return value


def safe_preview(value: Any, limit: int = 1000) -> str:
    redacted = _redact(value)
    if isinstance(redacted, str):
        rendered = redacted.replace("\r", "\\r").replace("\n", "\\n")
    else:
        try:
            rendered = json.dumps(
                redacted,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            rendered = repr(redacted)
    if len(rendered) <= limit:
        return rendered
    return rendered[: max(0, limit - 3)].rstrip() + "..."


def compact_log_value(
    value: Any,
    *,
    string_limit: int = 500,
    item_limit: int = 20,
) -> Any:
    value = _redact(value)
    if isinstance(value, str):
        if len(value) <= string_limit:
            return value.replace("\r", "\\r").replace("\n", "\\n")
        return (
            value[: max(0, string_limit - 3)]
            .rstrip()
            .replace("\r", "\\r")
            .replace("\n", "\\n")
            + "..."
        )
    if isinstance(value, Mapping):
        items = list(value.items())
        compact = {
            str(key): compact_log_value(
                item,
                string_limit=string_limit,
                item_limit=item_limit,
            )
            for key, item in items[:item_limit]
        }
        if len(items) > item_limit:
            compact["_omitted"] = len(items) - item_limit
        return compact
    if isinstance(value, (list, tuple)):
        compact = [
            compact_log_value(
                item,
                string_limit=string_limit,
                item_limit=item_limit,
            )
            for item in value[:item_limit]
        ]
        if len(value) > item_limit:
            compact.append(f"... {len(value) - item_limit} more")
        return compact
    return value


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(
            compact_log_value(value),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    rendered = safe_preview(value)
    if _SAFE_TOKEN.fullmatch(rendered):
        return rendered
    return json.dumps(rendered, ensure_ascii=False)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    message: str | None = None,
    exc_info: Any = None,
    **fields: Any,
) -> None:
    logger.log(
        level,
        message or event,
        exc_info=exc_info,
        extra={
            "momoi_event": event,
            "momoi_context": current_log_context(),
            "momoi_fields": {
                key: value for key, value in fields.items() if value is not None
            },
        },
    )


class KeyValueFormatter(logging.Formatter):
    def __init__(self, *, color: bool = False) -> None:
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).astimezone().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        level = record.levelname.ljust(7)
        event = getattr(record, "momoi_event", None)
        if not event:
            prefix = f"{timestamp} {level} {record.name}"
            message = record.getMessage().replace("\r", "\\r").replace("\n", "\\n")
            if record.exc_info:
                exception = safe_preview(self.formatException(record.exc_info), 2000)
                rendered = f"{prefix} {message} exception={_format_value(exception)}"
            else:
                rendered = f"{prefix} {message}".rstrip()
            return self._colorize(record, rendered)

        fields: dict[str, Any] = {}
        fields.update(getattr(record, "momoi_context", {}) or {})
        fields.update(getattr(record, "momoi_fields", {}) or {})
        if record.getMessage() and record.getMessage() != event:
            fields["message"] = record.getMessage()
        if record.exc_info:
            fields["exception"] = safe_preview(
                self.formatException(record.exc_info), 2000
            )
        ordered = [
            key for key in _PREFERRED_FIELDS if key in fields
        ] + sorted(key for key in fields if key not in _PREFERRED_FIELDS)
        rendered = " ".join(
            f"{key}={_format_value(fields[key])}"
            for key in ordered
        )
        group = _EVENT_GROUPS.get(event, record.name.rsplit(".", 1)[-1].upper())
        prefix = f"{timestamp} {level} {group:<9} event={event}"
        return self._colorize(record, f"{prefix} {rendered}".rstrip())

    def _colorize(self, record: logging.LogRecord, value: str) -> str:
        color = _LEVEL_COLORS.get(record.levelno) if self.color else None
        return f"{color}{value}{_COLOR_RESET}" if color else value


def configure_logging(level: int) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        KeyValueFormatter(color="NO_COLOR" not in os.environ)
    )
    logging.basicConfig(level=level, handlers=[handler], force=True)
