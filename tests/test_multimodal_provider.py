import copy

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from momoi.integrations.media import text_only_messages
from momoi.integrations.validation import llm_config
from momoi.integrations.adapters.openai import openai_messages
from momoi.integrations.adapters.anthropic import merge_adjacent_roles
from momoi.integrations.adapters.openai import OpenAIProvider
from momoi.integrations.adapters.anthropic import AnthropicProvider
from momoi.integrations.configuration import parse_provider_catalog
from momoi.integrations.registry import ServiceRegistry
from momoi.integrations.image_routing import ImageRoutingModel
from momoi.models import ProviderResponse
from momoi.llm.errors import ProviderResponseError
from momoi.observability.context import log_context, current_log_context
from momoi.observability.events import TRACE


def test_multimodal_defaults_and_validation():
    options = {"base_url": "https://example.com/v1", "model": "test"}
    assert llm_config(options, "openai").multimodal is True
    assert llm_config({**options, "multimodal": False}, "anthropic").multimodal is False
    for value in ("false", 0, None):
        with pytest.raises(ValueError):
            llm_config({**options, "multimodal": value}, "openai")


def test_image_prompt_upgrade_preserves_existing_and_dashboard_edits(tmp_path):
    from momoi.config.workspace import bootstrap
    from momoi.config.manager import ConfigurationManager
    from momoi.dashboard.settings import DashboardSettings

    config_path = tmp_path / "config.json"
    bootstrap(config_path)
    manager = ConfigurationManager(config_path)
    prompt_path = tmp_path / "prompts/IMAGE_RECOGNITION.md"
    default = prompt_path.read_text()
    prompt_path.unlink()
    config = manager.validate()
    assert prompt_path.read_text() == default
    settings = DashboardSettings.from_config(config)
    assert settings.read("image_recognition")["filename"] == "IMAGE_RECOGNITION.md"
    settings.update("image_recognition", "自定义识别要求")
    manager.validate()
    assert prompt_path.read_text() == "自定义识别要求"
    with pytest.raises(ValueError):
        settings.update("image_recognition", "")
    prompt_path.write_text("")
    manager.validate()
    assert prompt_path.read_text() == ""


def test_image_prompt_hot_reload_invalidates_cached_recognition(tmp_path):
    async def run():
        class Model:
            usage_sink = thinking_sink = usage_parser = None

            async def complete(self, *args, **kwargs):
                return ProviderResponse(content=[{"type": "text", "text": "cat"}], tool_calls=[])

        primary, vision = Model(), Model()
        vision.complete = AsyncMock(side_effect=vision.complete)
        path = tmp_path / "IMAGE_RECOGNITION.md"
        path.write_text("First prompt")
        router = ImageRoutingModel(primary, vision, prompt_path=path)
        messages = [{"role": "user", "content": [{"type": "image", "source": {"type": "url", "url": "https://example.com/cat"}}]}]
        await router.complete("System", messages)
        await router.complete("System", messages)
        assert vision.complete.await_count == 1
        assert vision.complete.await_args.args[0] == "First prompt"
        path.write_text("Updated prompt")
        await router.complete("System", messages)
        assert vision.complete.await_count == 2
        assert vision.complete.await_args.args[0] == "Updated prompt"

    asyncio.run(run())


def test_text_only_omits_images_without_mutating_history_or_tool_pairs():
    image = {"type": "image", "source": {"type": "url", "url": "https://example.com/picture.png"}}
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "Look at this"}, image]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "read", "name": "read_image", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read", "content": [image]}]},
    ]
    original = copy.deepcopy(messages)
    converted = text_only_messages(messages)
    assert messages == original
    assert converted[1] == original[1]
    assert converted[2]["content"][0]["tool_use_id"] == "read"
    for wire in (openai_messages("System", converted), merge_adjacent_roles(converted)):
        assert "picture.png" not in str(wire)
        assert "当前模型不支持多模态" in str(wire)
    assert "picture.png" in str(openai_messages("System", messages))


@pytest.mark.parametrize("adapter,provider", [("openai", OpenAIProvider), ("anthropic", AnthropicProvider)])
@pytest.mark.parametrize("multimodal", [False, True])
def test_transport_honors_multimodal_setting(adapter, provider, multimodal):
    async def run():
        captured = []

        async def respond(request):
            captured.append(await request.json())
            if adapter == "openai":
                return web.json_response({"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})
            return web.json_response({"content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn"})

        app = web.Application()
        app.router.add_post("/{path:.*}", respond)
        async with TestServer(app) as server:
            config = llm_config({"base_url": str(server.make_url("/v1")), "model": "test",
                                 "multimodal": multimodal}, adapter)
            messages = [{"role": "user", "content": [{"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": "AAAA"}}]}]
            async with provider(config) as client:
                await client.complete("System", messages)
            wire = str(captured[0]["messages"])
            assert ("AAAA" in wire) is multimodal
            assert ("当前模型不支持多模态" in wire) is not multimodal
            assert messages[0]["content"][0]["type"] == "image"

    asyncio.run(run())


def test_image_routing_preserves_tools_reuses_evidence_and_propagates_errors():
    async def run():
        def fake(text):
            class Model:
                usage_sink = thinking_sink = usage_parser = request_metrics_sink = None
                complete = AsyncMock(return_value=ProviderResponse(content=[{"type": "text", "text": text}], tool_calls=[]))
            return Model()

        primary, vision = fake("Answer"), fake("A cat, with the text HELLO")
        router = ImageRoutingModel(primary, vision)
        router.usage_sink = lambda **kwargs: None
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
        messages = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read",
                                                    "content": [image, {"type": "text", "text": "image id=123"}]}]}]
        await router.complete("System", messages, [], require_tool=True, required_tool="reply")
        await router.complete("System", messages)
        assert vision.complete.await_count == 1
        assert vision.usage_sink is router.usage_sink
        args, kwargs = primary.complete.await_args_list[0]
        result = args[1][0]["content"][0]
        assert result["tool_use_id"] == "read"
        assert "A cat" in result["content"][0]["text"]
        assert result["content"][1]["text"] == "image id=123"
        assert kwargs == {"require_tool": True, "required_tool": "reply"}
        assert messages[0]["content"][0]["content"][0] is image
        failed = ImageRoutingModel(fake("Answer"), fake(""))
        with pytest.raises(ProviderResponseError):
            await failed.complete("System", messages)
        failed.primary.complete.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("multimodal", [True, False])
def test_registry_owns_both_models_and_bypasses_recognition_for_multimodal(multimodal, tmp_path, caplog):
    caplog.set_level(TRACE, logger="momoi.llm.dumps")
    async def run():
        requests = []
        thinking, usage = [], []

        async def respond(request):
            body = await request.json()
            requests.append(body)
            return web.json_response({"choices": [{"message": {"role": "assistant", "content": "Visible cat"}, "finish_reason": "stop"}],
                                      "usage": {"prompt_tokens": 10, "completion_tokens": 3}})

        app = web.Application()
        app.router.add_post("/{path:.*}", respond)
        async with TestServer(app) as server:
            raw = {"version": 1, "credentials": {}, "services": {
                "primary": {"adapter": "openai", "settings": {"base_url": str(server.make_url("/v1")), "model": "primary", "multimodal": multimodal}},
                "vision": {"adapter": "openai", "settings": {"base_url": str(server.make_url("/v1")), "model": "vision"}},
            }, "bindings": {"llm": {"service": "primary"}, "vision": {"service": "vision"}}}
            registry = ServiceRegistry(parse_provider_catalog(raw, tmp_path / "providers.yaml"), dump_dir=tmp_path / "dumps")
            model = registry.llm
            model.thinking_sink = lambda **record: thinking.append(record)
            model.usage_sink = lambda **record: usage.append(record)
            assert isinstance(model, ImageRoutingModel) is not multimodal
            async with registry:
                with log_context(stage="owner", turn_id="test-turn", call_id="primary-call", round=1, channel="test"):
                    await model.complete("System", [{"role": "user", "content": [{"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": "AAAA"}}]}])
                    assert current_log_context()["call_id"] == "primary-call"
            assert [item["model"] for item in requests] == (["primary"] if multimodal else ["vision", "primary"])
            assert ("AAAA" in str(requests[-1])) is multimodal
            if not multimodal:
                assert "Visible cat" in str(requests[-1])
                assert thinking[0]["stage"] == "image_recognition"
                assert thinking[0]["call_id"] != "primary-call"
                assert thinking[0]["trace"]["parent_call_id"] == "primary-call"
                assert thinking[0]["turn_id"] == "test-turn"
                assert usage[0]["stage"] == "image_recognition"
                assert usage[0]["model"] == "vision"
                dump = json.loads((tmp_path / "dumps" / thinking[0]["trace"]["dump_file"]).read_text())
                assert dump["context"]["stage"] == "image_recognition"
                assert dump["context"]["parent_call_id"] == "primary-call"
                assert "response" in dump
                assert "omitted 4 base64 chars" in str(dump)
            assert thinking[-1]["stage"] == "owner"
            assert thinking[-1]["call_id"] == "primary-call"
            assert usage[-1]["model"] == "primary"
            assert registry._stack is None

    asyncio.run(run())
