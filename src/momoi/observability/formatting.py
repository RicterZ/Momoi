import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .events import TRACE
from .values import format_log_value, safe_preview

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
    "context_recall": "RECALL",
    "context_recall_memory_results": "RECALL",
    "context_recall_episode_results": "RECALL",
    "context_recall_state_results": "RECALL",
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
    "episode_deferred_cleanup": "ANNEALING",
    "prompt_reload_failed": "SERVICE",
}


class KeyValueFormatter(logging.Formatter):
    def __init__(self, timezone: ZoneInfo, *, color: bool = False) -> None:
        super().__init__()
        self.timezone = timezone
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, self.timezone).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        level = record.levelname.ljust(7)
        event = getattr(record, "momoi_event", None)
        if not event:
            prefix = f"{timestamp} {level} {record.name}"
            message = record.getMessage().replace("\r", "\\r").replace("\n", "\\n")
            if record.exc_info:
                exception = safe_preview(self.formatException(record.exc_info), 2000)
                rendered = f"{prefix} {message} exception={format_log_value(exception)}"
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
        ordered = [key for key in _PREFERRED_FIELDS if key in fields] + sorted(
            key for key in fields if key not in _PREFERRED_FIELDS
        )
        rendered = " ".join(f"{key}={format_log_value(fields[key])}" for key in ordered)
        group = _EVENT_GROUPS.get(event, record.name.rsplit(".", 1)[-1].upper())
        prefix = f"{timestamp} {level} {group:<9} event={event}"
        return self._colorize(record, f"{prefix} {rendered}".rstrip())

    def _colorize(self, record: logging.LogRecord, value: str) -> str:
        color = _LEVEL_COLORS.get(record.levelno) if self.color else None
        return f"{color}{value}{_COLOR_RESET}" if color else value


def configure_logging(level: int, timezone: ZoneInfo) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        KeyValueFormatter(timezone, color="NO_COLOR" not in os.environ)
    )
    logging.basicConfig(level=level, handlers=[handler], force=True)
