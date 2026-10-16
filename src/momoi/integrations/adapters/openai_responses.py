"""Stateless OpenAI Responses protocol, including DeepSeek compatibility."""
import copy
import json
from typing import Any

from .protocol_base import ProtocolProvider
from .openai import openai_messages, _text
from ...models import ProviderResponse, ToolCall
from ...llm.errors import ProviderResponseError
from ...llm.transport import responses_url


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


class OpenAIResponsesProvider(ProtocolProvider):
    protocol = "openai_responses"
    label = "OpenAI Responses-compatible"

    def _url(self) -> str:
        return responses_url(self.config.base_url)

    def _payload(self, system, messages, tools, *, require_tool, required_tool, effort):
        config = self.config
        payload: dict[str, Any] = {
            "model": config.model, "instructions": _text(system, "\n\n"),
            "input": responses_input(system, messages), "max_output_tokens": config.max_tokens,
            "store": False,
        }
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
        return payload

    def _parse(self, data, response) -> ProviderResponse:
        return parse_output(data)
