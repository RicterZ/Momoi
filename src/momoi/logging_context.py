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
    "event",
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
    logging.DEBUG: "\033[36m",
    logging.INFO: "\033[32m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[1;31m",
}
_COLOR_RESET = "\033[0m"


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


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
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
        prefix = f"{timestamp} {record.levelname} {record.name}"
        event = getattr(record, "momoi_event", None)
        if not event:
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
        fields["event"] = event
        if record.getMessage() and record.getMessage() != event:
            fields["message"] = record.getMessage()
        if record.exc_info:
            fields["exception"] = safe_preview(
                self.formatException(record.exc_info), 2000
            )
        ordered = [
            key for key in _PREFERRED_FIELDS if key in fields
        ] + sorted(key for key in fields if key not in _PREFERRED_FIELDS)
        rendered = " ".join(f"{key}={_format_value(fields[key])}" for key in ordered)
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
