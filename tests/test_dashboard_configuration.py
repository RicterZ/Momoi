import asyncio
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml
from aiohttp.test_utils import TestClient, TestServer

from momoi.config.manager import ConfigurationManager, RevisionConflict
from momoi.config.models import ConfigError
from momoi.config.workspace import atomic_write, bootstrap
from momoi.dashboard.app import create_dashboard_app
from momoi.dashboard.auth import issue_dashboard_jwt
from momoi.dashboard.settings import DashboardSettings
from momoi.runtime.supervisor import RuntimeSupervisor
from momoi.storage import Store, MemoryRecallQuery


LLM = {
    "adapter": "openai",
    "enabled": True,
    "options": {
        "base_url": "https://example.com/v1",
        "model": "test-model",
        "api_key": "private-key",
    },
}


class ConfigurationManagerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.json"
        bootstrap(self.path)
        self.manager = ConfigurationManager(self.path)

    def test_bootstrap_is_idempotent_and_loads_without_model_or_channels(self):
        before = {
            path: path.read_bytes()
            for path in self.path.parent.rglob("*")
            if path.is_file()
        }
        self.assertFalse(bootstrap(self.path))
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        config = self.manager.validate()
        self.assertFalse(config.providers.enabled("llm"))
        self.assertTrue(config.providers.enabled("embedding"))
        self.assertEqual(config.channel_configs, ())
        self.assertGreaterEqual(len(config.dashboard.token), 32)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_bootstrap_seeds_editable_embedding_once(self):
        path = self.path.parent / "compose" / "config.json"
        self.assertTrue(bootstrap(path))
        manager = ConfigurationManager(path)
        config = manager.validate()
        self.assertFalse(config.providers.enabled("llm"))
        self.assertEqual(config.channel_configs, ())
        snapshot = manager.snapshot()
        embedding = snapshot["capabilities"]["embedding"]
        self.assertTrue(embedding["enabled"])
        self.assertEqual(embedding["adapter"], "openai")
        self.assertEqual(embedding["options"]["endpoint"], "http://embedding:8002/v1/embeddings")
        self.assertEqual(embedding["options"]["model"], "BAAI/bge-small-zh-v1.5")
        self.assertEqual(embedding["options"]["dimensions"], 512)
        self.assertEqual(embedding["options"]["calibration_profile"], "bge-small-zh-v1.5-momoi-v1")
        embedding["enabled"] = False
        embedding["options"]["endpoint"] = "https://embedding.example/v1/embeddings"
        manager.save_binding("embedding", embedding, snapshot["revision"])
        before = manager.provider_path.read_bytes()
        self.assertFalse(bootstrap(path))
        self.assertEqual(manager.provider_path.read_bytes(), before)
        self.assertFalse(manager.validate().providers.enabled("embedding"))

    def test_bootstrap_preserves_existing_provider_file(self):
        path = self.path.parent / "existing" / "config.json"
        provider_path = path.parent / "providers.yaml"
        original = b"version: 1\ncredentials: {}\nservices: {}\nbindings: {}\n"
        provider_path.parent.mkdir()
        provider_path.write_bytes(original)
        self.assertTrue(bootstrap(path))
        self.assertEqual(provider_path.read_bytes(), original)
        self.assertFalse(ConfigurationManager(path).validate().providers.enabled("embedding"))

    def test_schema_plaintext_credentials_and_revision_conflict(self):
        before = self.manager.snapshot()
        saved = self.manager.save_binding("llm", LLM, before["revision"])
        self.assertIn("private-key", json.dumps(saved["providers"]["credentials"]))
        self.assertEqual(
            saved["capabilities"]["llm"]["options"]["api_key"], "private-key"
        )
        self.assertTrue(
            any(
                item["fields"].get("api_key", {}).get("secret")
                for item in saved["adapters"]
            )
        )
        changed = copy.deepcopy(saved["capabilities"]["llm"])
        changed["options"]["model"] = "another-model"
        self.manager.save_binding("llm", changed, saved["revision"])
        options = self.manager.validate().providers.options_for("llm")
        self.assertEqual(options["api_key"], "private-key")
        self.assertEqual(options["model"], "another-model")
        with self.assertRaises(RevisionConflict):
            self.manager.save_binding("llm", changed, saved["revision"])

    def test_invalid_save_and_failed_replace_leave_file_unchanged(self):
        revision = self.manager.revision()
        invalid = copy.deepcopy(LLM)
        invalid["options"]["timeout_seconds"] = -1
        with self.assertRaises(ConfigError):
            self.manager.save_binding("llm", invalid, revision)
        self.assertEqual(self.manager.revision(), revision)
        with patch(
            "momoi.config.workspace.os.replace", side_effect=OSError("disk error")
        ):
            with self.assertRaises(OSError):
                self.manager.save_binding("llm", LLM, revision)
        self.assertEqual(self.manager.revision(), revision)
        self.assertEqual(list(self.path.parent.glob(".providers.yaml.*")), [])

    def test_binding_edits_do_not_change_shared_service_or_env_credentials(self):
        raw = {
            "version": 1,
            "credentials": {"shared": {"api_key": {"env": "TEST_SHARED_KEY"}}},
            "services": {
                "shared": {"adapter": "openai", "credentials": "shared", "base_url": "https://api.deepseek.com"},
                "account": {"adapter": "deepseek", "credentials": "shared"},
            },
            "bindings": {
                "llm": {"service": "shared", "options": {"model": "test"}},
                "balance": {"service": "account"},
            },
        }
        atomic_write(self.manager.provider_path, yaml.safe_dump(raw))
        with patch.dict(os.environ, {"TEST_SHARED_KEY": "env-private-key"}):
            snapshot = self.manager.snapshot()
            changed = snapshot["capabilities"]["llm"]
            changed["options"]["model"] = "updated"
            self.manager.save_binding("llm", changed, snapshot["revision"])
            saved = self.manager.read_providers()
            self.assertEqual(saved["services"]["shared"], raw["services"]["shared"])
            self.assertEqual(
                saved["credentials"]["shared"], raw["credentials"]["shared"]
            )
            self.assertEqual(saved["bindings"]["balance"], raw["bindings"]["balance"])
            self.assertEqual(
                self.manager.validate().providers.options_for("balance")["api_key"],
                "env-private-key",
            )
            self.assertNotIn("env-private-key", self.manager.provider_path.read_text())

    def test_http_cannot_add_plugin_imports_or_edit_dashboard_auth(self):
        snapshot = self.manager.snapshot()
        document = snapshot["providers"]
        document["plugins"] = ["arbitrary_module"]
        with self.assertRaisesRegex(ConfigError, "locally"):
            self.manager.save("providers", document, snapshot["revision"])
        with self.assertRaisesRegex(ConfigError, "runtime settings"):
            self.manager.save(
                "app", {"dashboard": {"token": "x"}}, snapshot["revision"]
            )

    def test_required_sections_cannot_be_disabled_or_removed(self):
        saved = self.manager.save_binding("llm", LLM, self.manager.revision())
        revision = saved["revision"]
        with self.assertRaisesRegex(ConfigError, "model cannot be disabled"):
            self.manager.save_binding("llm", {**LLM, "enabled": False}, revision)
        raw = self.manager.read_providers()
        del raw["bindings"]["llm"]
        with self.assertRaisesRegex(ConfigError, "model cannot be removed"):
            self.manager.save("providers", raw, revision)
        saved = self.manager.save_runtime(
            {"channels": {"primary": "weixin", "enabled": {"weixin": {}}}}, revision
        )
        for document in (
            {"channels": {"primary": "", "enabled": {}}},
            {"channels": None},
        ):
            with self.assertRaises(ConfigError):
                self.manager.save_runtime(document, saved["revision"])
        app = saved["app"]
        del app["channels"]
        with self.assertRaisesRegex(ConfigError, "channels cannot be disabled"):
            self.manager.save("app", app, saved["revision"])

    def test_section_patch_preserves_other_runtime_settings(self):
        saved = self.manager.save_runtime(
            {"heartbeat": {"enabled": True}}, self.manager.revision()
        )
        self.manager.save_runtime(
            {"channels": {"primary": "weixin", "enabled": {"weixin": {}}}},
            saved["revision"],
        )
        self.assertTrue(self.manager.validate().heartbeat.enabled)

    def test_control_patch_preserves_existing_intervals_and_time(self):
        initial = {
            "heartbeat": {"enabled": True, "initial_delay_seconds": 11, "min_interval_seconds": 120, "max_interval_seconds": 360},
            "reflection": {"enabled": True, "at": "22:15"},
            "episode_annealing": {"enabled": True, "idle_seconds": 9, "max_seconds": 500},
        }
        self.manager.save_runtime(initial, self.manager.revision())
        self.manager.save_runtime(
            {section: {"enabled": False} for section in initial},
            self.manager.revision(),
        )
        saved = self.manager.read_app()
        for section, original in initial.items():
            self.assertEqual(saved[section], {**original, "enabled": False})
        self.manager.save_runtime({"reflection": {"at": "00:00"}}, self.manager.revision())
        self.assertFalse(self.manager.validate().reflection.enabled)
        self.assertEqual(self.manager.validate().reflection.at, "00:00")

    def test_app_fields_expose_enum_types_defaults_and_are_isolated(self):
        snapshot = self.manager.snapshot()
        fields = snapshot["app_fields"]
        self.assertEqual(set(fields), {"heartbeat", "logging", "reflection", "episode_annealing", "thinking"})
        self.assertEqual(fields["logging"]["fields"]["level"]["enum"], ["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        self.assertEqual(fields["reflection"]["fields"]["at"]["format"], "time")
        self.assertEqual(snapshot["app"]["reflection"]["at"], "03:00")
        self.assertTrue(snapshot["app"]["episode_annealing"]["enabled"])
        self.assertTrue(snapshot["app"]["heartbeat"]["enabled"])
        fields["logging"]["fields"]["level"]["enum"].append("INVALID")
        self.assertNotIn("INVALID", self.manager.snapshot()["app_fields"]["logging"]["fields"]["level"]["enum"])

    def test_voice_batch_is_atomic_and_keeps_credentials_when_disabled(self):
        voice = {
            "asr": {
                "adapter": "tencent",
                "enabled": True,
                "options": {"secret_id": "id", "secret_key": "secret"},
            },
            "tts": {
                "adapter": "fish",
                "enabled": True,
                "options": {"api_key": "key", "reference_id": "voice"},
            },
        }
        saved = self.manager.save_bindings(voice, self.manager.revision())
        invalid = copy.deepcopy(voice)
        invalid["asr"]["enabled"] = False
        invalid["tts"]["options"]["format"] = "invalid"
        with self.assertRaises(ConfigError):
            self.manager.save_bindings(invalid, saved["revision"])
        self.assertEqual(self.manager.revision(), saved["revision"])
        self.assertTrue(self.manager.validate().providers.enabled("asr"))
        for item in voice.values():
            item["enabled"] = False
        saved = self.manager.save_bindings(voice, saved["revision"])
        catalog = self.manager.validate().providers
        self.assertFalse(catalog.enabled("asr"))
        self.assertFalse(catalog.enabled("tts"))
        self.assertEqual(catalog.options_for("tts")["api_key"], "key")
        self.assertEqual(saved["capabilities"]["tts"]["options"]["api_key"], "key")

    def test_managed_credentials_shared_by_another_service_are_not_overwritten(self):
        self.manager.save_binding("llm", LLM, self.manager.revision())
        raw = self.manager.read_providers()
        raw["services"]["account"] = {
            "adapter": "deepseek",
            "credentials": "configured_llm",
        }
        raw["bindings"]["balance"] = {"service": "account"}
        atomic_write(self.manager.provider_path, yaml.safe_dump(raw))
        snapshot = self.manager.snapshot()
        changed = snapshot["capabilities"]["llm"]
        changed["options"]["api_key"] = "replacement-key"
        self.manager.save_binding("llm", changed, snapshot["revision"])
        catalog = self.manager.validate().providers
        self.assertEqual(catalog.options_for("llm")["api_key"], "replacement-key")
        self.assertEqual(catalog.options_for("balance")["api_key"], "private-key")


class FakeDaemon:
    instances = []

    def __init__(self, config):
        self.config = config
        self.ready = asyncio.Event()
        self.closed = False
        self.services = SimpleNamespace(balance=None)
        self.instances.append(self)

    async def run(self, stop):
        try:
            if self.config.providers.options_for("llm")["model"] == "startup-failure":
                raise RuntimeError("private runtime details")
            self.ready.set()
            await stop.wait()
        finally:
            self.closed = True


class DashboardConfigurationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.json"
        bootstrap(self.path)
        self.manager = ConfigurationManager(self.path)
        config = self.manager.dashboard_config()
        self.store = Store(config.database, config.workspace)
        self.addCleanup(self.store.close)
        FakeDaemon.instances = []
        self.runtime = RuntimeSupervisor(self.manager, factory=FakeDaemon)
        self.runtime.active_config = config
        self.addAsyncCleanup(self.runtime._retire)
        self.client = TestClient(
            TestServer(
                create_dashboard_app(
                    self.store,
                    token=config.dashboard.token,
                    settings=DashboardSettings.from_config(config),
                    configuration=self.manager,
                    runtime=self.runtime,
                )
            )
        )
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.auth = "Bearer " + issue_dashboard_jwt(config.dashboard.token)

    def enable(self, model="test-model"):
        llm = copy.deepcopy(LLM)
        llm["options"]["model"] = model
        snapshot = self.manager.save_binding("llm", llm, self.manager.revision())
        app = snapshot["app"]
        app["channels"] = {
            "primary": "napcat",
            "enabled": {"napcat": {"url": "ws://localhost", "owner_qq": "123"}},
        }
        self.manager.save("app", app, snapshot["revision"])

    async def test_zero_configuration_dashboard_auth_save_and_conflict(self):
        response = await self.client.get("/api/settings/configuration")
        self.assertEqual(response.status, 401)
        self.client.session.headers["Authorization"] = self.auth
        response = await self.client.get("/api/settings/configuration")
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(set(data["capabilities"]), {"embedding"})
        self.assertTrue(data["capabilities"]["embedding"]["enabled"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        await self.runtime.apply()
        self.assertEqual(self.runtime.status()["missing"], ["llm", "channel"])
        response = await self.client.put(
            "/api/settings/providers/llm",
            json={"revision": data["revision"], "document": LLM},
        )
        self.assertEqual(response.status, 200)
        self.assertIn("private-key", await response.text())
        response = await self.client.put(
            "/api/settings/providers/llm",
            json={"revision": data["revision"], "document": LLM},
        )
        self.assertEqual(response.status, 409)

    async def test_disabled_optional_sections_remove_runtime_services_but_keep_memory_tools(
        self,
    ):
        from momoi.runtime.daemon import MomoiDaemon

        self.enable()
        optional = {
            "asr": {"adapter": "tencent", "enabled": False, "options": {}},
            "tts": {"adapter": "fish", "enabled": False, "options": {}},
            "embedding": {"adapter": "openai", "enabled": False, "options": {}},
            "balance": {"adapter": "deepseek", "enabled": False, "options": {}},
        }
        self.client.session.headers["Authorization"] = self.auth
        response = await self.client.put(
            "/api/settings/providers",
            json={"revision": self.manager.revision(), "document": optional},
        )
        self.assertEqual(response.status, 200)
        daemon = MomoiDaemon(self.manager.validate())
        self.addCleanup(daemon.store.close)
        self.assertIsNone(daemon.asr_provider)
        self.assertIsNone(daemon.bubble_delivery.tts_provider)
        self.assertIsNone(daemon.services.balance)
        self.assertIsNone(daemon.services.embedding)
        names = {item["name"] for item in daemon.tool_surface.conversation_specs()}
        self.assertNotIn("send_voice", names)
        self.assertIn("memory_search", names)
        self.assertIn("recall", names)
        with daemon.store._db:
            daemon.store._db.execute("""INSERT INTO memories
                (kind, key, content, activation, authority, source_event_id, evidence_quote,
                 importance, created_at, updated_at)
                VALUES ('shared', 'section-test', 'remember section-test', 'recall', 'owner',
                        'event', 'remember section-test', 0.5, 1, 1)""")
        query = MemoryRecallQuery("section-test")
        evidence = await daemon.semantic_recall.prepare([query])
        self.assertEqual(evidence.fallback_reason, "disabled")
        self.assertFalse(await daemon.semantic_recall.maintain_once())
        recalled = daemon.store.rank_recalled_memories(
            [query], 6, dense_evidence=evidence
        )
        self.assertEqual(recalled[0]["key"], "section-test")

    async def test_channel_patch_rejects_disable_through_http(self):
        self.enable()
        self.client.session.headers["Authorization"] = self.auth
        response = await self.client.patch(
            "/api/settings/configuration/app",
            json={
                "revision": self.manager.revision(),
                "document": {"channels": {"primary": "", "enabled": {}}},
            },
        )
        self.assertEqual(response.status, 400)

    async def test_mcp_get_returns_raw_config_and_patch_saves_and_requests_apply(self):
        path = self.path.parent / "mcp.json"
        content = '{\n  "mcpServers": {"test": {"disabled": true, "env": {"TOKEN": "${UNSET_TOKEN}"}}}\n}\n'
        path.write_text(content)
        for method in ("get", "patch"):
            response = await getattr(self.client, method)("/api/settings/mcp")
            self.assertEqual(response.status, 401)
        self.client.session.headers["Authorization"] = self.auth
        response = await self.client.get("/api/settings/mcp")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "application/json")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(await response.text(), content)
        revision = self.manager.revision()
        etag = response.headers["ETag"]
        self.runtime.changed.clear()
        response = await self.client.patch("/api/settings/mcp", json={"mcpServers": {}}, headers={"If-Match": etag})
        self.assertEqual(response.status, 202)
        self.assertEqual((await response.json())["revision"], self.manager.revision())
        self.assertEqual(json.loads(path.read_text()), {"mcpServers": {}})
        self.assertNotEqual(self.manager.revision(), revision)
        self.assertTrue(self.runtime.changed.is_set())
        response = await self.client.patch("/api/settings/mcp", json={"mcpServers": {}}, headers={"If-Match": etag})
        self.assertEqual(response.status, 409)
        path.unlink()
        self.runtime.changed.clear()
        response = await self.client.patch("/api/settings/mcp", data="not JSON")
        self.assertEqual(response.status, 400)
        self.assertFalse(path.exists())
        self.assertFalse(self.runtime.changed.is_set())
        response = await self.client.get("/api/settings/mcp")
        self.assertEqual(await response.json(), {"mcpServers": {}})

    async def test_mcp_patch_validation_preserves_files_and_enables_existing_workspace(self):
        self.client.session.headers["Authorization"] = self.auth
        path = self.manager.mcp_path()
        before = path.read_bytes()
        for value in ({}, [], {"mcpServers": []}, {"mcpServers": {"bad": 1}},
                      {"mcpServers": {"bad": {"command": "demo", "disabled": "false"}}},
                      {"mcpServers": {"bad": {"command": "demo", "args": "--arg"}}}):
            response = await self.client.patch("/api/settings/mcp", json=value)
            self.assertEqual(response.status, 400, value)
            self.assertEqual(path.read_bytes(), before)
        app = self.manager.read_app()
        app["tools"]["mcp_config"] = None
        atomic_write(self.path, json.dumps(app))
        path.unlink()
        document = {"mcpServers": {"later": {"disabled": True}}, "extra": "preserved"}
        response = await self.client.patch("/api/settings/mcp", json=document)
        self.assertEqual(response.status, 202)
        self.assertEqual(json.loads(path.read_text()), document)
        self.assertEqual(self.manager.validate().mcp_config, path)

    async def test_mcp_patch_saves_to_custom_path(self):
        self.client.session.headers["Authorization"] = self.auth
        app = self.manager.read_app()
        app["tools"]["mcp_config"] = "nested/tools.json"
        atomic_write(self.path, json.dumps(app))
        response = await self.client.patch("/api/settings/mcp", json={"mcpServers": {}})
        self.assertEqual(response.status, 202)
        self.assertEqual(self.manager.mcp_path().read_text(), '{\n  "mcpServers": {}\n}\n')

    async def test_mcp_patch_cannot_overwrite_app_or_provider_catalog(self):
        self.client.session.headers["Authorization"] = self.auth
        for path in (self.path, self.manager.provider_path):
            app = self.manager.read_app()
            app["tools"]["mcp_config"] = str(path)
            atomic_write(self.path, json.dumps(app))
            before = path.read_bytes()
            response = await self.client.patch("/api/settings/mcp", json={"mcpServers": {}})
            self.assertEqual(response.status, 400)
            self.assertEqual(path.read_bytes(), before)

    async def test_mcp_get_uses_configured_path(self):
        self.client.session.headers["Authorization"] = self.auth
        path = self.path.parent / "custom-mcp.json"
        path.write_text('{"mcpServers": {}, "extra": "preserved"}')
        for reference in (path.name, str(path.resolve())):
            app = self.manager.read_app()
            app["tools"]["mcp_config"] = reference
            atomic_write(self.path, json.dumps(app))
            response = await self.client.get("/api/settings/mcp")
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.text(), path.read_text())
        path.unlink()
        response = await self.client.get("/api/settings/mcp")
        self.assertEqual(response.status, 404)

    async def test_runtime_controls_validate_atomically_through_http(self):
        self.client.session.headers["Authorization"] = self.auth
        for section, key, invalid in (
            ("logging", "level", "anything"),
            ("logging", "level", "info"),
            ("logging", "level", 20),
            ("logging", "level", None),
            ("heartbeat", "enabled", "false"),
            ("reflection", "enabled", 1),
            ("episode_annealing", "enabled", None),
            ("reflection", "at", "24:00"),
            ("reflection", "at", "03:60"),
            ("reflection", "at", "3:00"),
            ("reflection", "at", ""),
            ("reflection", "at", None),
            ("reflection", "time", "03:00"),
        ):
            with self.subTest(section=section, key=key, invalid=invalid):
                revision = self.manager.revision()
                before = self.path.read_bytes()
                response = await self.client.patch(
                    "/api/settings/configuration/app",
                    json={"revision": revision, "document": {section: {key: invalid}}},
                )
                self.assertEqual(response.status, 400, await response.text())
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.manager.revision(), revision)
        for level in ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            response = await self.client.patch(
                "/api/settings/configuration/app",
                json={"revision": self.manager.revision(), "document": {"logging": {"level": level}}},
            )
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual((await response.json())["app"]["logging"]["level"], level)

    async def test_runtime_controls_reload_after_http_save_and_file_edit(self):
        import logging

        self.enable()
        self.client.session.headers["Authorization"] = self.auth
        logger = logging.getLogger()
        self.addCleanup(logger.setLevel, logger.level)
        stop = asyncio.Event()
        task = asyncio.create_task(self.runtime.run(stop))

        async def applied(revision):
            async with asyncio.timeout(5):
                while self.runtime.applied_revision != revision:
                    await asyncio.sleep(0.01)

        try:
            await applied(self.manager.revision())
            first = self.runtime.daemon
            response = await self.client.get("/api/settings/configuration")
            snapshot = await response.json()
            self.assertIn("enum", snapshot["app_fields"]["logging"]["fields"]["level"])
            response = await self.client.patch(
                "/api/settings/configuration/app",
                json={"revision": snapshot["revision"], "document": {
                    "heartbeat": {"enabled": True},
                    "logging": {"level": "TRACE"},
                    "reflection": {"enabled": True, "at": "21:45"},
                    "episode_annealing": {"enabled": True},
                }},
            )
            self.assertEqual(response.status, 200, await response.text())
            saved = await response.json()
            await applied(saved["revision"])
            self.assertTrue(first.closed)
            config = self.runtime.daemon.config
            self.assertTrue(config.heartbeat.enabled)
            self.assertTrue(config.reflection.enabled)
            self.assertEqual(config.reflection.at, "21:45")
            self.assertTrue(config.episode_annealing.enabled)
            self.assertEqual(logger.level, logging.TRACE)
            # File edits use the same validation and watcher, without a reload request.
            changed = self.manager.read_app()
            for section in ("heartbeat", "reflection", "episode_annealing"):
                changed[section]["enabled"] = False
            changed["logging"]["level"] = "INFO"
            changed["reflection"]["at"] = "23:59"
            atomic_write(self.path, json.dumps(changed))
            await applied(self.manager.revision())
            config = self.runtime.daemon.config
            self.assertFalse(config.heartbeat.enabled)
            self.assertFalse(config.reflection.enabled)
            self.assertFalse(config.episode_annealing.enabled)
            self.assertEqual(config.reflection.at, "23:59")
            self.assertEqual(logger.level, logging.INFO)
            response = await self.client.get("/api/settings/runtime")
            status = await response.json()
            self.assertEqual(status["state"], "running")
            self.assertEqual(status["applied_revision"], self.manager.revision())
        finally:
            stop.set()
            await task

    async def test_runtime_thinking_stage_schema_validation_and_partial_save(self):
        self.client.session.headers["Authorization"] = self.auth
        response = await self.client.get("/api/settings/configuration")
        snapshot = await response.json()
        fields = snapshot["app_fields"]["thinking"]["fields"]["stages"]
        self.assertFalse(fields["advanced"])
        self.assertEqual(fields["properties"]["episode_anneal"]["enum"], ["", "low", "medium", "high", "xhigh", "max"])
        for adapter in snapshot["adapters"]:
            if adapter["capability"] == "llm" and adapter["adapter"] in {"openai", "anthropic"}:
                self.assertEqual(set(adapter["fields"]["thinking"]["properties"]), {"effort"})
                self.assertEqual(adapter["fields"]["thinking"]["properties"]["effort"]["enum"], ["", "low", "medium", "high", "xhigh", "max"])
        providers_before = self.manager.provider_path.read_bytes()

        async def save(stages):
            return await self.client.patch("/api/settings/configuration/app", json={
                "revision": self.manager.revision(),
                "document": {"thinking": {"stages": stages}},
            })

        response = await save({"episode_anneal": "low", "reply_followup": "low"})
        self.assertEqual(response.status, 200, await response.text())
        response = await save({"episode_anneal": "max"})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.manager.validate().thinking_stages, {"episode_anneal": "max", "reply_followup": "low"})
        response = await save({"episode_anneal": ""})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.manager.validate().thinking_stages, {"reply_followup": "low"})
        self.assertEqual(self.manager.provider_path.read_bytes(), providers_before)
        for effort in ("low", "medium", "high", "xhigh", "max", ""):
            response = await save({"owner": effort})
            self.assertEqual(response.status, 200)
            self.assertEqual(self.manager.validate().thinking_stages.get("owner", ""), effort)
        for invalid in (None, [], {"unknown": "low"}, {"owner": "invalid"}, {"owner": False}, {"owner": None}):
            before = self.path.read_bytes()
            response = await save(invalid)
            self.assertEqual(response.status, 400, invalid)
            self.assertEqual(self.path.read_bytes(), before)
        self.enable()
        for protocol in ("anthropic", "openai"):
            document = copy.deepcopy(LLM)
            document["adapter"] = protocol
            self.manager.save_binding("llm", document, self.manager.revision())
            self.assertEqual(self.manager.validate().thinking_stages, {"reply_followup": "low"})

    async def test_runtime_thinking_stages_reload_from_api_and_file(self):
        self.enable()
        self.client.session.headers["Authorization"] = self.auth
        stop = asyncio.Event()
        task = asyncio.create_task(self.runtime.run(stop))

        async def applied():
            async with asyncio.timeout(5):
                while self.runtime.applied_revision != self.manager.revision():
                    await asyncio.sleep(0.01)

        try:
            await applied()
            first = self.runtime.daemon
            response = await self.client.patch("/api/settings/configuration/app", json={
                "revision": self.manager.revision(),
                "document": {"thinking": {"stages": {"episode_anneal": "low", "reply_followup": "low"}}},
            })
            self.assertEqual(response.status, 200)
            await applied()
            self.assertTrue(first.closed)
            self.assertEqual(self.runtime.daemon.config.thinking_stages, {"episode_anneal": "low", "reply_followup": "low"})
            second = self.runtime.daemon
            app = self.manager.read_app()
            app["thinking"]["stages"]["episode_anneal"] = "high"
            atomic_write(self.path, json.dumps(app))
            await applied()
            self.assertTrue(second.closed)
            self.assertEqual(self.runtime.daemon.config.thinking_stages["episode_anneal"], "high")
        finally:
            stop.set()
            await task

    async def test_generation_replacement_invalid_reload_rollback_and_disable(self):
        self.enable()
        await self.runtime.apply()
        first = self.runtime.daemon
        revision = self.runtime.applied_revision
        self.assertEqual(self.runtime.state, "running")
        original = self.manager.provider_path.read_text()
        atomic_write(self.manager.provider_path, "version: [broken")
        await self.runtime.apply()
        self.assertIs(self.runtime.daemon, first)
        self.assertFalse(first.closed)
        self.assertEqual(self.runtime.applied_revision, revision)
        atomic_write(self.manager.provider_path, original)
        self.enable("startup-failure")
        await self.runtime.apply()
        self.assertTrue(first.closed)
        self.assertEqual(self.runtime.state, "error")
        self.assertTrue(self.runtime.status()["runtime_active"])
        self.assertIn("previous configuration restored", self.runtime.error)
        self.assertNotIn("private runtime details", self.runtime.error)
        self.assertEqual(self.runtime.applied_revision, revision)
        self.enable("updated-model")
        await self.runtime.apply()
        second = self.runtime.daemon
        self.assertEqual(self.runtime.state, "running")
        self.assertEqual(
            second.config.providers.options_for("llm")["model"], "updated-model"
        )
        saved = self.manager.snapshot()
        llm = saved["capabilities"]["llm"]
        llm["enabled"] = False
        with self.assertRaisesRegex(ConfigError, "model cannot be disabled"):
            self.manager.save_binding("llm", llm, saved["revision"])
        self.assertFalse(second.closed)
        self.assertEqual(self.runtime.state, "running")

    async def test_file_watcher_applies_without_an_http_reload_request(self):
        stop = asyncio.Event()
        task = asyncio.create_task(self.runtime.run(stop))

        async def until(predicate):
            async with asyncio.timeout(4):
                while not predicate():
                    await asyncio.sleep(0.01)

        try:
            await until(lambda: bool(self.runtime.applied_revision))
            self.enable()
            await until(lambda: self.runtime.state == "running")
            self.assertEqual(self.runtime.applied_revision, self.manager.revision())
            instance = self.runtime.daemon
            atomic_write(self.manager.provider_path, "version: [broken")
            await until(lambda: self.runtime.state == "error")
            self.assertIs(self.runtime.daemon, instance)
            self.assertFalse(instance.closed)
        finally:
            stop.set()
            await task
        self.assertTrue(instance.closed)

    async def test_weixin_login_uses_async_verification_without_terminal(self):
        saved = self.manager.snapshot()
        app = saved["app"]
        app["channels"] = {"primary": "weixin", "enabled": {"weixin": {}}}
        self.manager.save("app", app, saved["revision"])
        self.client.session.headers["Authorization"] = self.auth

        async def fake_login(config, *, on_update, verification):
            await on_update("waiting", qr_content="https://example.com/login")
            await asyncio.sleep(0.01)
            await on_update("verification_required")
            self.assertEqual(await verification(), "123456")
            await on_update("confirmed")

        with patch("momoi.dashboard.configuration.login", fake_login):
            response = await self.client.post("/api/settings/channels/weixin/login")
            self.assertEqual(response.status, 202)
            for _ in range(30):
                state = await (await self.client.get("/api/settings/runtime")).json()
                if state["weixin_login"]["status"] == "verification_required":
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(self.runtime.suspended)
            response = await self.client.post(
                "/api/settings/channels/weixin/verify", json={"code": "123456"}
            )
            self.assertEqual(response.status, 200)
            await asyncio.sleep(0.01)
            state = await (await self.client.get("/api/settings/runtime")).json()
            self.assertEqual(state["weixin_login"]["status"], "confirmed")
            self.assertFalse(self.runtime.suspended)


if __name__ == "__main__":
    unittest.main()
