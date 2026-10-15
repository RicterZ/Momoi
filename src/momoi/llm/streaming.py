"""Incremental provider events with the existing complete-response parsers."""
import asyncio
import copy
import json
import logging
from time import monotonic
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import AsyncIterator
from typing import Any, Literal

from .errors import ProviderResponseError
from ..observability.events import log_event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderEvent:
    type: Literal["text_delta", "reasoning_delta", "tool_delta", "signature_delta", "item_start", "provider_delta", "response"]
    delta: str = ""
    index: int = 0
    data: Any = None


_stream = ContextVar("provider_stream", default=None)


class StreamingProvider:
    async def stream(self, *args, **kwargs) -> AsyncIterator[ProviderEvent]:
        """Yield text/reasoning/tool deltas, then one response event.

        Consumers stopping early must close the iterator (use aclosing).
        Tool argument deltas are not executable until the final response.
        """
        queue = asyncio.Queue(maxsize=1)

        async def run():
            token = _stream.set(queue.put)
            try:
                result = await self.complete(*args, **kwargs)
                await queue.put(ProviderEvent("response", data=result))
            finally:
                _stream.reset(token)

        task = asyncio.create_task(run())
        try:
            while True:
                receive = asyncio.create_task(queue.get())
                try:
                    done, _ = await asyncio.wait((receive, task), return_when=asyncio.FIRST_COMPLETED)
                    if receive in done:
                        event = receive.result()
                        yield event
                        if event.type == "response":
                            await task
                            return
                    elif queue.empty():
                        await task
                        return
                finally:
                    receive.cancel()
                    await asyncio.gather(receive, return_exceptions=True)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def stream_payload(payload, protocol):
    if _stream.get() is not None:
        payload["stream"] = True
        if protocol == "openai":
            payload["stream_options"] = {"include_usage": True}


async def sse_data(content):
    """Parse SSE lines independently of TCP chunk boundaries and CRLF."""
    data = []
    async for raw in content:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        yield "\n".join(data)


async def response_data(response, protocol):
    emit = _stream.get()
    started = monotonic()
    if emit is None:
        result = await response.json()
        log_event(logger, logging.INFO, "llm_response_body_complete", protocol=protocol,
                  streaming=False, body_read_ms=round((monotonic() - started) * 1000))
        return result
    original_emit = emit
    seen = set()

    async def emit(event):
        if event.type not in seen:
            seen.add(event.type)
            log_event(logger, logging.INFO, "llm_first_stream_event", protocol=protocol,
                      event_type=event.type,
                      since_headers_ms=round((monotonic() - started) * 1000))
        await original_emit(event)
    result = {"choices": [{"message": {"content": "", "tool_calls": []}}]} if protocol == "openai" else {}
    tools, blocks, arguments = {}, {}, {}
    terminal = False
    async with aclosing(sse_data(response.content)) as events:
        async for raw in events:
            if raw == "[DONE]":
                terminal = True
                break
            try:
                event = json.loads(raw)
            except ValueError as error:
                raise ProviderResponseError("Invalid stream JSON") from error
            if not isinstance(event, dict) or event.get("error") or event.get("type") == "error":
                raise ProviderResponseError("Provider stream returned an error")
            if protocol == "openai":
                if event.get("usage"):
                    result["usage"] = event["usage"]
                for choice in event.get("choices", []):
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta", {})
                    message = result["choices"][0]["message"]
                    for key, kind in (("content", "text_delta"), ("reasoning_content", "reasoning_delta"), ("reasoning", "reasoning_delta")):
                        if isinstance(delta.get(key), str):
                            message[key] = message.get(key, "") + delta[key]
                            await emit(ProviderEvent(kind, delta[key]))
                    for part in delta.get("tool_calls", []):
                        index = part["index"]
                        tool = tools.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        tool["id"] += part.get("id", "")
                        for key in ("name", "arguments"):
                            tool["function"][key] += part.get("function", {}).get(key, "")
                        await emit(ProviderEvent("tool_delta", part.get("function", {}).get("arguments", ""), index, part))
                    if choice.get("finish_reason"):
                        result["choices"][0]["finish_reason"] = choice["finish_reason"]
                result["choices"][0]["message"]["tool_calls"] = [tools[i] for i in sorted(tools)]
            elif protocol == "openai_responses":
                kind = event.get("type", "")
                if kind in {"response.failed", "response.incomplete"}:
                    raise ProviderResponseError("Responses stream did not complete")
                if kind == "response.completed":
                    result = event["response"]
                    terminal = True
                    break
                elif kind.endswith(".delta"):
                    category = ("text_delta" if kind == "response.output_text.delta" else
                                "tool_delta" if kind == "response.function_call_arguments.delta" else
                                "reasoning_delta" if "reasoning" in kind else "provider_delta")
                    await emit(ProviderEvent(category, event.get("delta", ""), event.get("output_index", 0), event))
                elif kind == "response.output_item.added":
                    await emit(ProviderEvent("item_start", index=event.get("output_index", 0), data=event.get("item")))
            else:
                kind = event.get("type")
                index = event.get("index", 0)
                if kind == "message_start":
                    result = copy.deepcopy(event["message"])
                elif kind == "content_block_start":
                    blocks[index] = copy.deepcopy(event["content_block"])
                    await emit(ProviderEvent("item_start", index=index, data=copy.deepcopy(blocks[index])))
                elif kind == "content_block_delta":
                    delta = event["delta"]
                    block = blocks[index]
                    for key, category in (("text", "text_delta"), ("thinking", "reasoning_delta"), ("signature", "signature_delta")):
                        if key in delta:
                            block[key] = block.get(key, "") + delta[key]
                            await emit(ProviderEvent(category, delta[key], index))
                    if "partial_json" in delta:
                        arguments[index] = arguments.get(index, "") + delta["partial_json"]
                        await emit(ProviderEvent("tool_delta", delta["partial_json"], index))
                    if delta.get("type") == "citations_delta":
                        block.setdefault("citations", []).append(delta["citation"])
                elif kind == "message_delta":
                    result.update(event.get("delta", {}))
                    result.setdefault("usage", {}).update(event.get("usage", {}))
                elif kind == "message_stop":
                    terminal = True
                    break
    if not terminal:
        raise ProviderResponseError("Provider stream ended before completion")
    if protocol == "anthropic":
        for index, raw in arguments.items():
            try:
                blocks[index]["input"] = json.loads(raw)
            except ValueError as error:
                raise ProviderResponseError("Invalid streamed tool arguments") from error
        result["content"] = [blocks[i] for i in sorted(blocks)]
    return result
