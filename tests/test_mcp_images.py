"""Exercise MCP images through execution and actual provider HTTP requests."""
import asyncio
import base64
import io
import json
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image

from momoi.integrations.adapters.anthropic import AnthropicProvider
from momoi.integrations.adapters.openai import OpenAIProvider
from momoi.integrations.adapters.openai_responses import OpenAIResponsesProvider
from momoi.integrations.validation import llm_config
from momoi.llm.dumps import redact_dump_media, redact_mcp_tool_data
from momoi.models import ProviderResponse, ToolCall, TurnDraft
from momoi.observability.values import compact_log_value
from momoi.runtime import MomoiDaemon
from momoi.runtime.agent.harness import TurnHarness
from momoi.runtime.agent.tool_batch import ToolBatchRequest, ToolBatchState
from momoi.runtime.agent.workflow import TurnExecutionSpec
from momoi.runtime.transcript.native import render_exchanges
from momoi.mcp.content import store_content_images
from momoi.storage.conversation.images import MAX_IMAGE_BYTES
from tests.support import config


def picture(color="red", format="JPEG"):
    output = io.BytesIO()
    Image.new("RGB", (3, 3), color).save(output, format=format)
    return {"type": "image", "mimeType": Image.MIME[format],
            "data": base64.b64encode(output.getvalue()).decode()}


async def execute_batch(daemon, blocks, *, is_error=False):
    name = "mcp__example__capture"
    call = ToolCall("capture-1", name, {})
    response = ProviderResponse([
        {"type": "tool_use", "id": call.id, "name": name, "input": {}}
    ], [call])
    messages = []

    async def empty(*args):
        return []

    request = ToolBatchRequest(
        response=response, messages=messages, request_tools=[{"name": name}],
        tools=[], enable_tool_groups={}, current_events=[], draft=TurnDraft(),
        harness=TurnHarness.for_stage("owner"), execution=TurnExecutionSpec("owner"),
        source_event_id="", turn_id="images", call_id="round-1", round_number=1,
        delivery_channel=daemon.channel, heartbeat_owner_event_revision=None,
        workflow=None, state=ToolBatchState(), prepare_heartbeat_context=empty,
        submit_owner_context=empty, settle_owner_updates=empty,
    )
    # Mock only the MCP SDK boundary; manager serialization and runtime are real.
    sdk = SimpleNamespace(is_error=is_error, model_dump=lambda **_: {"content": blocks, "isError": is_error})
    daemon.mcp._sessions["example"] = type("Session", (), {"call_tool": AsyncMock(return_value=sdk)})()

    async def invoke(*args):
        return await daemon.mcp._invoke("example", "capture", {})

    with patch.object(daemon.mcp, "has_tool", return_value=True), \
            patch.object(daemon.mcp, "capability", return_value="read"), \
            patch.object(daemon.mcp, "call", side_effect=invoke):
        await daemon.tool_batch.execute(request)
    return messages


@pytest.mark.parametrize("adapter,provider", [
    ("openai", OpenAIProvider), ("anthropic", AnthropicProvider),
    ("openai_responses", OpenAIResponsesProvider),
])
@pytest.mark.parametrize("multimodal", [True, False])
def test_mcp_images_reach_model_request_and_history(tmp_path, adapter, provider, multimodal):
    daemon = MomoiDaemon(config(str(tmp_path)))
    daemon.store.begin_turn("images", "owner", [])
    jpeg, png = picture(), picture("blue", "PNG")

    async def run():
        messages = await execute_batch(daemon, [
            {"type": "text", "text": "Frame at 1 second"}, jpeg, png, jpeg,
        ])
        assert len([b for b in messages[-1]["content"] if b["type"] == "image"]) == 2
        result = json.loads(messages[-1]["content"][0]["content"])
        assert len(result["image_ids"]) == 2
        assert "Frame at 1 second" in str(result)
        assert jpeg["data"] not in str(result)
        exchanges = daemon.store.turn_exchanges(["images"])["images"]
        replay = render_exchanges(exchanges)
        assert "capture-1" in str(replay)
        assert "use read_image" in str(replay)
        for identifier in result["image_ids"]:
            assert identifier in str(replay)
            assert daemon.store.read_image(identifier)["type"] == "image"
        assert jpeg["data"] not in str(exchanges)
        snapshot = daemon.tool_results.historical_payload(result["result_ref"])
        assert jpeg["data"] not in str(snapshot)
        assert snapshot["image_ids"] == result["image_ids"]
        requests = []

        async def respond(request):
            requests.append(await request.json())
            if adapter == "anthropic":
                return web.json_response({"content": [{"type": "text", "text": "OK"}]})
            if adapter == "openai_responses":
                return web.json_response({"output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}]})
            return web.json_response({"choices": [{"message": {"content": "OK"}}]})

        app = web.Application()
        app.router.add_post("/{path:.*}", respond)
        async with TestServer(app) as server:
            options = llm_config({"base_url": str(server.make_url("/v1")),
                                  "model": "mock", "multimodal": multimodal}, adapter)
            async with provider(options) as client:
                await client.complete("System", messages)
                await client.complete("System", messages)  # later rounds retain images once each
            for payload in requests:
                wire = payload.get("messages", payload.get("input"))
                assert "Frame at 1 second" in str(wire)
                assert str(wire).count(jpeg["data"]) == int(multimodal)
                assert str(wire).count(png["data"]) == int(multimodal)
                assert "capture-1" in str(wire)
                assert "Frame at 1 second" not in str(redact_mcp_tool_data(payload))
                if not multimodal:
                    assert "当前模型不支持多模态" in str(wire)
                    continue
                if adapter == "openai":
                    assert [m["role"] for m in wire] == ["system", "assistant", "tool", "user"]
                    assert wire[2]["tool_call_id"] == "capture-1"
                    images = [b for b in wire[3]["content"] if b["type"] == "image_url"]
                    assert images[0]["image_url"]["url"] == "data:image/jpeg;base64," + jpeg["data"]
                elif adapter == "openai_responses":
                    assert [m.get("type") for m in wire[:2]] == ["function_call", "function_call_output"]
                    assert wire[1]["call_id"] == "capture-1"
                    assert wire[2]["content"][1] == {"type": "input_image", "image_url": "data:image/jpeg;base64," + jpeg["data"]}
                else:
                    assert wire[1]["content"][0]["tool_use_id"] == "capture-1"
                    assert wire[1]["content"][2]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": jpeg["data"]}
                assert jpeg["data"] not in str(redact_dump_media(payload))
        assert jpeg["data"] not in str(compact_log_value(jpeg))
    try:
        asyncio.run(run())
    finally:
        daemon.store.close()


@pytest.mark.parametrize("error", [False, True])
def test_mcp_text_and_error_results_keep_pairing(tmp_path, error):
    daemon = MomoiDaemon(config(str(tmp_path)))
    daemon.store.begin_turn("images", "owner", [])
    async def run():
        messages = await execute_batch(daemon, [{"type": "text", "text": "tool evidence"}], is_error=error)
        block = messages[-1]["content"][0]
        assert block["tool_use_id"] == "capture-1"
        assert block["is_error"] is error
        assert "tool evidence" in block["content"]
        assert all(b["type"] != "image" for b in messages[-1]["content"])
    try:
        asyncio.run(run())
    finally:
        daemon.store.close()


def test_invalid_images_and_mime_are_explicit_and_never_kept_as_text(tmp_path):
    daemon = MomoiDaemon(config(str(tmp_path)))
    valid = picture()
    corrupt = bytearray(base64.b64decode(picture(format="PNG")["data"]))
    corrupt[corrupt.find(b"IDAT") + 4] ^= 1
    malformed = [
        {"type": "image", "mimeType": "image/png", "data": base64.b64encode(corrupt).decode()},
        {**valid, "data": "not base64"},
        {**valid, "data": base64.b64encode(b"not an image").decode()},
        {**valid, "mimeType": "image/png"},
        {**valid, "mimeType": "image/svg+xml"},
        {**valid, "data": "A" * (MAX_IMAGE_BYTES * 4 // 3 + 5)},
    ]
    try:
        raw, ids = store_content_images({"ok": True, "result": {"content": malformed}}, daemon.store)
        assert not ids
        assert str(raw["result"]).count("MCP image unavailable") == len(malformed)
        assert "not base64" not in str(raw)
        assert valid["data"] not in str(raw)
        assert not daemon.store._db.execute("SELECT id FROM visual_images").fetchall()
    finally:
        daemon.store.close()


def test_mcp_references_survive_restart_and_structured_content(tmp_path):
    from momoi.storage import Store
    from momoi.runtime.agent.tool_executor import ToolExecutor
    from momoi.runtime.agent.result_store import ToolResultStore
    path = tmp_path / "restart.db"
    store = Store(path)
    executor = ToolExecutor(SimpleNamespace(workspace=tmp_path, database=path,
        tool_result_max_chars=1000), store, None, None, None, ToolResultStore(tmp_path / "results"))
    jpeg = picture()
    result = executor.normalize(ToolCall("c", "mcp__test__capture", {}), {
        "ok": False, "error": "mcp_tool_error", "result": {
            "content": [{"type": "text", "text": "caption"}, jpeg],
            "structuredContent": {"timestamp": 1},
        }}, "mcp")
    assert "caption" in str(result)
    assert result["structured_result"] == {"timestamp": 1}
    assert result["ok"] is False
    identifier = result["image_ids"][0]
    store.close()
    store = Store(path)
    try:
        assert store.read_image(identifier)["source"]["data"] == jpeg["data"]
        executor.store = store
        result = executor.normalize(ToolCall("d", "mcp__test__capture", {}), {
            "ok": True, "result": {"content": [
                *[{"type": "text", "text": "x" * 2000} for _ in range(20)],
                {**jpeg, "data": "invalid"},
            ]}}, "mcp")
        assert "MCP image unavailable" in result["image_warning"]
        assert result["truncated"]
    finally:
        store.close()


def test_trace_redaction_preserves_tool_schema_and_pairing():
    schema = {"properties": {"tool_call_id": {"type": "string"}}}
    payload = {"tools": [schema], "messages": [
        {"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "mcp__example__login"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "Cookie: fake; Bearer fake; https://example.invalid/qr-login"},
    ]}
    redacted = redact_mcp_tool_data(payload)
    assert redacted["tools"] == [schema]
    assert redacted["messages"][1]["tool_call_id"] == "a"
    assert "fake" not in str(redacted)
    assert "qr-login" not in str(redacted)
    assert "fake" in str(payload)
