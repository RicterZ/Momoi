import hashlib
import json
import logging
import re
from pathlib import Path
from time import monotonic, time
from typing import Any, Callable

from .dumps import dump_response
from ..integrations.models import LLMConfig
from ..llm.accounting import parse_protocol_usage
from ..observability.context import current_log_context
from ..observability.events import TRACE, log_event
from ..memory.text import estimate_tokens
from ..storage.ops.thinking import persist_thinking_failure

logger = logging.getLogger(__name__)
_tool_schema_hashes: dict[str, str] = {}
_MEMORY_SECTIONS = re.compile(r"<(triggered_memories|scoped_memories)>\s*(.*?)</\1>", re.DOTALL)


def injected_memory_context(messages: list[dict[str, Any]]) -> dict[str, str]:
    """Snapshot the memory sections actually present in this request, including interjections."""
    sections: dict[str, list[str]] = {"triggered_memories": [], "scoped_memories": []}
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content") or ""
        text = content if isinstance(content, str) else "".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        for name, value in _MEMORY_SECTIONS.findall(text):
            if value.strip():
                sections[name].append(value.strip())
    return {name: "\n\n".join(values) for name, values in sections.items()}


def usage_metrics(data: dict[str, Any]) -> dict[str, float | int | bool] | None:
    return parse_protocol_usage(data)


def log_usage(
    data: dict[str, Any],
    *,
    protocol: str,
    duration_ms: int,
    parse_usage: Callable[[dict[str, Any]], dict[str, float | int | bool] | None]
    | None = None,
) -> dict[str, float | int | bool] | None:
    metrics = (parse_usage or usage_metrics)(data)
    if not metrics:
        log_event(
            logger,
            TRACE,
            "llm_usage",
            protocol=protocol,
            available=False,
            duration_ms=duration_ms,
        )
        return None
    log_event(
        logger,
        TRACE,
        "llm_usage",
        protocol=protocol,
        duration_ms=duration_ms,
        input=metrics["input"],
        uncached=metrics["uncached"],
        cache_read=metrics["cache_read"],
        cache_write=metrics["cache_write"],
        output=metrics["output"],
        total=metrics["total"],
        cache_hit=round(float(metrics["cache_hit_rate"]), 1),
        cache_reported=metrics["cache_reported"],
    )
    return metrics


def persist_usage(
    sink: Callable[..., None] | None,
    metrics: dict[str, float | int | bool] | None,
    *,
    model: str,
    created_at: float | None = None,
) -> None:
    if sink is None or metrics is None:
        return
    context = current_log_context()
    try:
        sink(
            created_at=time() if created_at is None else created_at,
            turn_id=str(context.get("turn_id") or ""),
            stage=str(context.get("stage") or ""),
            model=model,
            metrics=metrics,
        )
    except Exception as error:
        log_event(
            logger,
            logging.WARNING,
            "llm_usage_record_failed",
            error_type=type(error).__name__,
            exc_info=True,
        )


def persist_thinking(
    sink: Callable[..., None] | None,
    *,
    reasoning: str,
    assistant_text: str = "",
    tools: list[str],
    model: str,
    tool_calls: list[dict[str, Any]] | None = None,
    dump_file: str = "",
    protocol: str = "",
    memory_context: dict[str, str] | None = None,
) -> None:
    if sink is None:
        return
    context = current_log_context()
    try:
        sink(
            created_at=time(),
            turn_id=str(context.get("turn_id") or ""),
            call_id=str(context.get("call_id") or ""),
            stage=str(context.get("stage") or ""),
            round=int(context.get("round") or 0),
            model=model,
            tools=tools,
            reasoning=reasoning,
            assistant_text=assistant_text,
            trace={"tool_calls": tool_calls or [], "dump_file": dump_file, "protocol": protocol,
                   **({"memory_context": memory_context} if memory_context is not None else {}),
                   **{key: context[key] for key in ("parent_call_id", "tool_call_id", "channel") if key in context}},
        )
    except Exception as error:
        persist_thinking_failure(error)


def openai_reasoning(message: dict[str, Any]) -> str:
    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def anthropic_reasoning(content: list[dict[str, Any]]) -> str:
    parts = [
        str(block.get("thinking") or block.get("text") or "")
        for block in content
        if isinstance(block, dict)
        and block.get("type") in {"thinking", "redacted_thinking"}
    ]
    return "\n".join(part for part in parts if part.strip())


def thinking_effort(config: LLMConfig) -> str:
    from ..integrations.request_context import requested_thinking_effort

    return requested_thinking_effort(config.thinking.effort)


def log_tool_schema(protocol: str, tools: object) -> None:
    values = tools if isinstance(tools, list) else []
    rendered = json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    names = [
        str(tool.get("name") or (tool.get("function") or {}).get("name") or "")
        for tool in values
        if isinstance(tool, dict)
    ]
    digest = hashlib.sha256(rendered.encode()).hexdigest()
    changed = _tool_schema_hashes.get(protocol) != digest
    _tool_schema_hashes[protocol] = digest
    log_event(
        logger,
        TRACE,
        "llm_tool_schema",
        protocol=protocol,
        tool_count=len(names),
        tool_schema_chars=len(rendered) if changed else None,
        tool_schema_tokens=estimate_tokens(rendered) if changed else None,
        tool_schema_sha256=digest,
        tool_names=names if changed else None,
    )


def record_response(
    data: dict[str, Any],
    *,
    protocol: str,
    attempt_started: float,
    dump_path: Path | None,
    usage_sink: Callable[..., None] | None,
    model: str,
    parse_usage: Callable[[dict[str, Any]], dict[str, float | int | bool] | None]
    | None = None,
    created_at: float | None = None,
) -> tuple[int, dict[str, float | int | bool] | None]:
    dump_response(dump_path, data)
    duration_ms = int((monotonic() - attempt_started) * 1000)
    metrics = log_usage(
        data, protocol=protocol, duration_ms=duration_ms, parse_usage=parse_usage
    )
    from .request_metrics import response_usage

    response_usage(metrics)
    persist_usage(usage_sink, metrics, model=model, created_at=created_at)
    return duration_ms, metrics


def compact_response_text(text: str) -> str:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return json.dumps(text, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
