import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from momoi.config.manager import ConfigurationManager
from momoi.config.workspace import bootstrap
from momoi.dashboard.app import create_dashboard_app
from momoi.dashboard.auth import issue_dashboard_jwt
from momoi.dashboard.settings import DashboardSettings
from momoi.integrations.registry import Adapter, adapter_schemas, register_adapter
from momoi.storage import Store


class ProviderConnectionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.json"
        bootstrap(self.path)
        self.manager = ConfigurationManager(self.path)
        config = self.manager.dashboard_config()
        self.store = Store(config.database, config.workspace)
        self.addCleanup(self.store.close)
        self.runtime = SimpleNamespace(request_apply=Mock())
        self.client = TestClient(TestServer(create_dashboard_app(
            self.store, token="test-secret", settings=DashboardSettings.from_config(config),
            configuration=self.manager, runtime=self.runtime,
        )))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer " + issue_dashboard_jwt("test-secret")}
        self.mode = {"status": 200}
        self.requests = []

        async def endpoint(request):
            payload = await request.json() if request.method == "POST" else None
            self.requests.append((request.path, payload, dict(request.headers)))
            if self.mode["status"] != 200:
                return web.json_response({"error": {"message": "upstream error"}}, status=self.mode["status"])
            if "payload" in self.mode:
                return web.json_response(self.mode["payload"])
            if request.path.endswith("/chat/completions"):
                return web.json_response({"choices": [{"message": {"role": "assistant", "content": "OK"}}]})
            if request.path.endswith("/messages"):
                return web.json_response({"content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn"})
            if request.path.endswith("/embeddings"):
                return web.json_response({"data": [{"index": 0, "embedding": [1, 0]}]})
            raise web.HTTPNotFound()

        upstream = web.Application()
        upstream.router.add_route("*", "/{path:.*}", endpoint)
        self.upstream = TestServer(upstream)
        await self.upstream.start_server()
        self.addAsyncCleanup(self.upstream.close)

    def options(self, capability):
        if capability == "llm":
            return {"base_url": str(self.upstream.make_url("/v1")), "api_key": "unsaved-key", "model": "unsaved-model"}
        if capability == "embedding":
            return {"endpoint": str(self.upstream.make_url("/v1/embeddings")), "model": "test-embedding", "dimensions": 2}
        return {}

    async def probe(self, capability="llm", adapter="openai", options=None, **extra):
        return await self.client.post(
            f"/api/settings/providers/{capability}/test", headers=self.headers,
            json={"adapter": adapter, "options": self.options(capability) if options is None else options, **extra},
        )

    async def test_real_builtin_requests_do_not_save_or_apply(self):
        before = {p: p.read_bytes() for p in self.path.parent.rglob("*") if p.is_file()}
        for capability, adapter in (("llm", "openai"), ("llm", "anthropic"), ("embedding", "openai")):
            response = await self.probe(capability, adapter, enabled=False)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            data = await response.json()
            self.assertTrue(data["ok"], data)
            self.assertEqual(data["capability"], capability)
            self.assertGreaterEqual(data["elapsed_ms"], 0)
        for path, payload, headers in self.requests:
            if path.endswith(("chat/completions", "messages")):
                self.assertEqual(payload["model"], "unsaved-model")
        self.assertEqual(len([r for r in self.requests if r[0].endswith("/embeddings")]), 2)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertFalse((self.path.parent / "llm-dumps").exists())
        self.assertEqual(self.store.dashboard_usage()["today"]["requests"], 0)
        self.runtime.request_apply.assert_not_called()

    async def test_auth_validation_and_unsupported_make_no_requests(self):
        for headers in ({}, {"Authorization": "Bearer invalid"}):
            response = await self.client.post("/api/settings/providers/llm/test", headers=headers, json={})
            self.assertEqual(response.status, 401)
        for options in ({}, {**self.options("llm"), "max_tokens": "wrong"}, {**self.options("llm"), "unknown": True}):
            response = await self.probe(options=options)
            self.assertEqual(response.status, 400)
            self.assertEqual((await response.json())["error"]["code"], "validation")
        response = await self.probe(adapter="unknown")
        self.assertEqual(response.status, 400)
        for capability, adapter in (("asr", "tencent"), ("tts", "fish"), ("balance", "deepseek")):
            response = await self.probe(capability, adapter, options={})
            self.assertEqual(response.status, 400)
            self.assertEqual((await response.json())["error"]["code"], "unsupported")
        self.assertEqual(self.requests, [])

        for content in ("not JSON", "[]", "null"):
            response = await self.client.post(
                "/api/settings/providers/llm/test", headers=self.headers,
                data=content,
            )
            self.assertEqual(response.status, 400)
            self.assertEqual((await response.json())["error"]["code"], "validation")

    async def test_network_failure_and_environment_credentials(self):
        options = self.options("llm")
        options["api_key"] = {"env": "MOMOI_PROBE_TEST_KEY"}
        with patch.dict("os.environ", {"MOMOI_PROBE_TEST_KEY": "resolved-test-key"}):
            data = await (await self.probe(options=options)).json()
        self.assertTrue(data["ok"])
        headers = {key.lower(): value for key, value in self.requests[-1][2].items()}
        self.assertEqual(headers["authorization"], "Bearer resolved-test-key")
        await self.upstream.close()
        options["api_key"] = "test-key"
        data = await (await self.probe(options=options)).json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"]["code"], "connection")

    async def test_http_errors_are_reported_without_retries(self):
        for status, code in ((401, "authentication"), (403, "authentication"), (404, "request"), (429, "rate_limit"), (500, "server")):
            self.mode["status"] = status
            count = len(self.requests)
            response = await self.probe()
            data = await response.json()
            self.assertEqual(response.status, 200)
            self.assertFalse(data["ok"])
            self.assertEqual(data["error"]["code"], code)
            self.assertEqual(data["error"]["http_status"], status)
            self.assertEqual(len(self.requests), count + 1)
        response = await self.probe("embedding", options=self.options("embedding"))
        data = await response.json()
        self.assertEqual(data["error"]["http_status"], 500)

    async def test_invalid_response_and_embedding_dimensions_are_failures(self):
        self.mode["payload"] = {"choices": []}
        data = await (await self.probe()).json()
        self.assertEqual(data["error"]["code"], "invalid_response")
        for vector in ([1, 0, 0], [0, 0]):
            self.mode["payload"] = {"data": [{"index": 0, "embedding": vector}]}
            data = await (await self.probe("embedding")).json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["error"]["code"], "invalid_response")

    async def test_isolated_probe_works_when_saved_configuration_is_broken(self):
        self.manager.provider_path.write_text("invalid providers")
        data = await (await self.probe()).json()
        self.assertTrue(data["ok"])
        self.assertEqual(self.manager.provider_path.read_text(), "invalid providers")

    async def test_custom_adapter_timeout_busy_and_cleanup(self):
        entered = asyncio.Event()
        instances = []

        class Custom:
            accounting = None

            def __init__(self, options, context):
                self.options = options
                self.transport = context.transport
                self.closed = False
                instances.append(self)

            async def balance(self):
                raise AssertionError("test callback should run")

            async def close(self):
                self.closed = True

        async def probe(instance):
            self.assertIsNotNone(instance.transport._session)
            entered.set()
            await asyncio.Event().wait()

        name = "probe_" + uuid.uuid4().hex
        register_adapter(Adapter(name, "balance", Custom, lambda options: None, {}, test=probe))
        with patch("momoi.integrations.testing.TEST_TIMEOUT_SECONDS", 0.2):
            first = asyncio.create_task(self.probe("balance", name, options={}))
            await asyncio.wait_for(entered.wait(), 1)
            response = await self.probe("balance", name, options={})
            self.assertEqual(response.status, 429)
            self.assertEqual((await response.json())["error"]["code"], "busy")
            data = await (await first).json()
            self.assertEqual(data["error"]["code"], "timeout")
        self.assertTrue(instances[0].closed)
        self.assertIsNone(instances[0].transport._session)
        self.assertTrue((await (await self.probe()).json())["ok"])

    async def test_metadata_describes_registered_test_support(self):
        schemas = {(a["capability"], a["adapter"]): a for a in adapter_schemas()}
        for capability, adapter in (("llm", "openai"), ("llm", "anthropic"), ("embedding", "openai")):
            self.assertTrue(schemas[capability, adapter]["test_supported"])
        for capability, adapter in (("asr", "tencent"), ("tts", "fish"), ("balance", "deepseek")):
            self.assertFalse(schemas[capability, adapter]["test_supported"])
