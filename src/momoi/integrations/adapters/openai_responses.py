from ...llm.streaming import response_data, stream_payload
"""Stateless OpenAI Responses protocol, including DeepSeek compatibility."""
import copy
import json
import aiohttp
from time import time
from typing import Any

from .openai_base import OpenAIProtocolProvider
from .openai import openai_messages, _text
from ..media import text_only_messages
from ...models import ProviderResponse, ToolCall
from ...llm.dumps import dump_request
from ...llm.errors import ProviderResponseError
from ...llm.request_metrics import RequestMetric, response_headers
from ...llm.telemetry import record_response, persist_thinking, thinking_effort, log_tool_schema
from ...llm.transport import retry_request, http_error, responses_url


def responses_input(system, messages):
    items = []
    for message in messages:
        continuation = message.get("provider_continuation", {}).get("openai_responses", {})
        if message.get("role") == "assistant" and isinstance(continuation.get("output"), list):
            items.extend(copy.deepcopy(continuation["output"]))
            continue
        for wire in openai_messages("", [message]):
            role = wire["role"]
            if role == "tool":
                items.append({"type": "function_call_output", "call_id": wire["tool_call_id"], "output": wire["content"]})
                continue
            content = wire.get("content")
            if isinstance(content, list):
                content = [
                    {"type": "input_image", "image_url": part["image_url"]["url"]}
                    if part["type"] == "image_url" else {"type": "input_text", "text": part["text"]}
                    for part in content
                ]
            if content:
                items.append({"role": role, "content": content})
            for call in wire.get("tool_calls", []):
                items.append({"type": "function_call", "call_id": call["id"], **call["function"]})
    return items


def parse_output(data):
    if data.get("error") or data.get("status") in {"failed", "incomplete", "cancelled", "in_progress", "queued"}:
        raise ProviderResponseError("Responses endpoint returned an unsuccessful response")
    output = data.get("output")
    if not isinstance(output, list):
        raise ProviderResponseError("Responses endpoint returned no output items")
    content, calls, reasoning = [], [], []
    for item in output:
        if not isinstance(item, dict):
            raise ProviderResponseError("Responses endpoint returned an invalid output item")
        kind = item.get("type")
        if item.get("status") == "incomplete":
            raise ProviderResponseError("Responses endpoint returned an incomplete output item")
        if kind == "message":
            blocks = item.get("content", [])
            if not isinstance(blocks, list) or any(not isinstance(b, dict) for b in blocks):
                raise ProviderResponseError("Responses endpoint returned invalid message content")
            for block in blocks:
                if block.get("type") == "output_text" and isinstance(block.get("text"), str) and block["text"].strip():
                    content.append({"type": "text", "text": block["text"]})
        elif kind == "reasoning":
            blocks = []
            for key in ("content", "summary"):
                value = item.get(key) or []
                if not isinstance(value, list) or any(not isinstance(b, dict) for b in value):
                    raise ProviderResponseError("Responses endpoint returned invalid reasoning content")
                blocks.extend(value)
            for block in blocks:
                if isinstance(block.get("text"), str):
                    reasoning.append(block["text"])
        elif kind == "function_call":
            if any(not isinstance(item.get(key), str) or not item[key].strip() for key in ("call_id", "name")):
                raise ProviderResponseError("Responses endpoint returned an invalid function call")
            error = None
            raw = item.get("arguments")
            try:
                if not isinstance(raw, str):
                    raise TypeError
                arguments = json.loads(raw)
            except json.JSONDecodeError:
                arguments, error = {}, "invalid_tool_arguments_json"
            except TypeError:
                arguments, error = {}, "tool_arguments_must_be_json"
            if not isinstance(arguments, dict):
                arguments, error = {}, "tool_arguments_must_be_object"
            call = ToolCall(item["call_id"], item["name"], arguments, error)
            calls.append(call)
            content.append({"type": "tool_use", "id": call.id, "name": call.name, "input": arguments})
        elif kind not in {"message", "reasoning"}:
            raise ProviderResponseError("Responses endpoint returned an unsupported output item")
    if not content:
        raise ProviderResponseError("Responses endpoint returned no text or function calls")
    return ProviderResponse(content, calls, reasoning="\n".join(reasoning),
                            continuation={"openai_responses": {"output": copy.deepcopy(output)}})


class OpenAIResponsesProvider(OpenAIProtocolProvider):
    protocol = "openai_responses"

    async def complete(self, system, messages, tools=None, *, require_tool=False, required_tool=None):
        if self._session is None:
            raise RuntimeError("provider is not started")
        config = self.config
        if not config.multimodal:
            messages = text_only_messages(messages)
        payload: dict[str, Any] = {
            "model": config.model, "instructions": _text(system, "\n\n"),
            "input": responses_input(system, messages), "max_output_tokens": config.max_tokens,
            "store": False,
        }
        effort = thinking_effort(config)
        if effort:
            payload["reasoning"] = {"effort": "none" if effort == "off" else effort}
        if not effort or effort == "off":
            payload["temperature"] = config.temperature
        # DeepSeek ignores include; OpenAI uses it for stateless reasoning replay.
        payload["include"] = ["reasoning.encrypted_content"]
        if tools:
            payload["tools"] = [{"type": "function", "name": tool["name"],
                                 "description": tool.get("description", ""),
                                 "parameters": tool["input_schema"], "strict": False} for tool in tools]
            if config.tool_choice and required_tool:
                payload["tool_choice"] = {"type": "function", "name": required_tool}
            elif config.tool_choice and require_tool:
                payload["tool_choice"] = "required"
        log_tool_schema(self.protocol, payload.get("tools"))
        stream_payload(payload, "openai_responses")
        dump_path = dump_request(self.dump_dir, self.protocol, payload, require_tool)

        async def request(attempt_started):
            billed_at = time()
            async with self._session.post(responses_url(config.base_url), json=payload,
                                         headers={"authorization": f"Bearer {config.api_key}",
                                                  "content-type": "application/json"}) as response:
                response_headers(response.status)
                if response.status != 200:
                    raise await http_error(response, "OpenAI Responses-compatible")
                try:
                    data = await response_data(response, "openai_responses")
                except (ValueError, aiohttp.ClientError) as error:
                    raise ProviderResponseError("Responses endpoint returned invalid JSON") from error
                if not isinstance(data, dict):
                    raise ProviderResponseError("Responses endpoint returned non-object JSON")
                _, metrics = record_response(data, protocol=self.protocol, attempt_started=attempt_started,
                                             dump_path=dump_path, usage_sink=self.usage_sink,
                                             model=config.model, parse_usage=self.usage_parser, created_at=billed_at)
                result = parse_output(data)
                persist_thinking(self.thinking_sink, reasoning=result.reasoning,
                                 assistant_text="\n".join(b["text"] for b in result.content if b["type"] == "text"),
                                 tools=[call.name for call in result.tool_calls],
                                 tool_calls=[{"id": call.id, "name": call.name, "arguments": call.arguments,
                                              **({"argument_error": call.argument_error} if call.argument_error else {})}
                                             for call in result.tool_calls],
                                 model=config.model, protocol=self.protocol,
                                 dump_file=dump_path.name if dump_path else "")
                return ProviderResponse(result.content, result.tool_calls, metrics,
                                        result.reasoning, result.continuation)

        return await retry_request(protocol=self.protocol, circuit=self._circuit,
                                   max_retries=config.max_retries, request_fields={"model": config.model},
                                   operation=request, monitor=RequestMetric(self.request_metrics_sink,
                                   payload=payload, protocol=self.protocol, endpoint=config.base_url))
