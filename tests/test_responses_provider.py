import asyncio
import copy

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from momoi.integrations.adapters.openai_responses import OpenAIResponsesProvider, parse_output, responses_input
from momoi.integrations.models import LLMConfig
from momoi.integrations.adapters.deepseek import DeepSeekAccounting
from momoi.integrations.registry import adapter_definition
from momoi.llm.accounting import parse_protocol_usage
from momoi.llm.errors import ProviderResponseError
from momoi.llm.request_metrics import request_shape
from momoi.llm.transport import responses_url
from momoi.runtime.agent.protocol import assistant_history_message


OUTPUT = [
    {"type": "reasoning", "id": "rs1", "content": [{"type": "reasoning_text", "text": "think"}], "encrypted_content": "opaque"},
    {"type": "function_call", "id": "item1", "call_id": "call1", "name": "read", "arguments": '{"path":"a"}'},
]


def test_replay_and_history_conversion():
    result = parse_output({"output": OUTPUT, "status": "completed"})
    assert result.tool_calls[0].id == "call1"
    assert result.reasoning == "think"
    messages = [assistant_history_message(result.content, result.continuation),
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call1", "content": "ok"}, {"type": "text", "text": "next"}]}]
    before = copy.deepcopy(messages)
    items = responses_input("system", messages)
    assert items[:2] == OUTPUT
    assert items[2] == {"type": "function_call_output", "call_id": "call1", "output": "ok"}
    assert items[3] == {"role": "user", "content": "next"}
    assert messages == before
    assert responses_input("", [assistant_history_message(result.content)])[0]["call_id"] == "call1"


@pytest.mark.parametrize("raw,error", [("{", "invalid_tool_arguments_json"), (None, "tool_arguments_must_be_json"), ("[]", "tool_arguments_must_be_object")])
def test_bad_arguments(raw, error):
    item = {**OUTPUT[1], "arguments": raw}
    assert parse_output({"output": [item]}).tool_calls[0].argument_error == error


@pytest.mark.parametrize("data", [{"output": []}, {"output": OUTPUT, "status": "incomplete"}, {"error": {"message": "bad"}}, {"output": [{"type": "custom_tool_call"}]}, {"output": [{"type": "message", "content": None}]}, {"output": [{"type": "reasoning", "content": "invalid"}]}])
def test_invalid_response(data):
    with pytest.raises(ProviderResponseError):
        parse_output(data)


def test_images_usage_metrics_and_registration():
    items = responses_input("", [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}}]}])
    assert items[0]["content"][0] == {"type": "input_image", "image_url": "data:image/png;base64,abc"}
    metrics = parse_protocol_usage({"usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 70}, "output_tokens": 10}})
    assert (metrics["input"], metrics["uncached"], metrics["cache_read"], metrics["total"]) == (100, 30, 70, 110)
    shape = request_shape({"input": items, "instructions": "system", "model": "test"})
    assert shape["message_count"] == 1
    assert "input" not in shape["settings_fields"]
    assert responses_url("https://api.deepseek.com") == "https://api.deepseek.com/v1/responses"
    assert responses_url("https://example.com/proxy/v1/") == "https://example.com/proxy/v1/responses"
    assert adapter_definition("openai_responses", "llm").schema["model"]["default"] == "deepseek-flash"


def test_http_tool_loop():
    async def scenario():
        payloads = []
        async def completion(request):
            payloads.append(await request.json())
            if len(payloads) == 1:
                return web.json_response({"status": "completed", "output": OUTPUT})
            return web.json_response({"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}],
                                     "usage": {"input_tokens": 20, "input_tokens_details": {"cached_tokens": 10}, "output_tokens": 2}})
        app = web.Application()
        app.router.add_post("/v1/responses", completion)
        async with TestServer(app) as server:
            config = LLMConfig(str(server.make_url('/')).rstrip('/'), "secret", "test", 100, 0.6, 10, 0, "openai_responses", True)
            async with OpenAIResponsesProvider(config) as provider:
                provider.usage_parser = DeepSeekAccounting().parse_usage
                messages = [{"role": "user", "content": "hi"}]
                tools = [{"name": "read", "input_schema": {"type": "object", "properties": {}}}]
                first = await provider.complete("system", messages, tools, required_tool="read")
                messages += [assistant_history_message(first.content, first.continuation), {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call1", "content": "ok"}]}]
                final = await provider.complete("system", messages)
                assert final.content == [{"type": "text", "text": "done"}]
                assert final.usage["cache_read"] == 10
                assert final.usage["uncached"] == 10
                assert final.usage["cache_reported"] is True
        assert payloads[0]["reasoning"] == {"effort": "low"}
        assert payloads[0]["tool_choice"] == {"type": "function", "name": "read"}
        assert payloads[0]["tools"][0]["strict"] is False
        assert payloads[0]["store"] is False
        assert "messages" not in payloads[0] and "thinking" not in payloads[0]
        assert payloads[1]["input"][1:3] == OUTPUT
    asyncio.run(scenario())
