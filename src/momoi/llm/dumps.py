import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..observability.events import TRACE, log_event
from ..observability.context import current_log_context

logger = logging.getLogger(__name__)


def dump_request(
    dump_dir: Path | None,
    provider: str,
    payload: dict[str, Any],
    require_tool: bool,
) -> Path | None:
    if dump_dir is None or not logger.isEnabledFor(TRACE):
        return None
    try:
        timestamp = datetime.now(timezone.utc)
        dump_dir.mkdir(parents=True, exist_ok=True)
        path = dump_dir / (
            f"{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex}.json"
        )
        safe_payload = redact_dump_media(redact_mcp_tool_data(payload))
        path.write_text(
            json.dumps(
                {
                    "timestamp": timestamp.isoformat(),
                    "provider": provider,
                    "context": current_log_context(),
                    "require_tool": require_tool,
                    "payload": safe_payload,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return path
    except (OSError, TypeError, ValueError) as error:
        log_event(
            logger,
            logging.WARNING,
            "llm_dump_failed",
            error_type=type(error).__name__,
            exc_info=True,
        )
        return None


def dump_response(path: Path | None, data: Any) -> None:
    if path is None:
        return
    try:
        dumped = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(dumped, dict):
            return
        dumped["response"] = redact_dump_media(data)
        path.write_text(
            json.dumps(dumped, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError) as error:
        log_event(
            logger,
            logging.WARNING,
            "llm_dump_failed",
            error_type=type(error).__name__,
            exc_info=True,
        )


def redact_dump_media(value: Any) -> Any:
    if isinstance(value, list):
        return [redact_dump_media(item) for item in value]
    if isinstance(value, dict):
        redacted = {key: redact_dump_media(item) for key, item in value.items()}
        if redacted.get("type") in {"base64", "image"} and isinstance(redacted.get("data"), str):
            redacted["data"] = f"[omitted {len(redacted['data'])} base64 chars]"
        return redacted
    if (
        isinstance(value, str)
        and value.startswith("data:image/")
        and ";base64," in value
    ):
        prefix, encoded = value.split(",", 1)
        return f"{prefix},[omitted {len(encoded)} base64 chars]"
    return value


def redact_mcp_tool_data(payload: Any) -> Any:
    """MCP text can contain login QR URLs or credentials; trace only pairing.

    Inspect call names rather than guessing which opaque returned URLs are
    sensitive. This applies to all three existing wire protocols.
    """
    identifiers = set()

    def collect(value):
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            if value.get("type") == "tool_use" and str(value.get("name", "")).startswith("mcp__"):
                identifiers.add(value.get("id"))
            if value.get("type") == "function_call" and str(value.get("name", "")).startswith("mcp__"):
                identifiers.add(value.get("call_id"))
            function = value.get("function")
            if isinstance(function, dict) and str(function.get("name", "")).startswith("mcp__"):
                identifiers.add(value.get("id"))
            for item in value.values():
                collect(item)

    def redact(value):
        if isinstance(value, list):
            return [redact(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: redact(item) for key, item in value.items()}
        identifier = value.get("tool_use_id") or value.get("tool_call_id") or value.get("call_id")
        if identifier in identifiers:
            if value.get("type") == "function_call_output":
                result["output"] = "[omitted untrusted MCP tool data]"
            elif value.get("type") == "tool_result" or value.get("role") == "tool":
                result["content"] = "[omitted untrusted MCP tool data]"
        return result

    collect(payload)
    identifiers.discard(None)
    return redact(payload)
