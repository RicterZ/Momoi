import asyncio
import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from momoi.config.manager import ConfigurationManager, KEEP_SECRET
from momoi.config.models import ConfigError
from momoi.config.workspace import bootstrap
from momoi.integrations.fields import normalize_fields, validate_schema
from momoi.integrations.registry import Adapter, ServiceRegistry, register_adapter
from momoi.dashboard.app import create_dashboard_app
from momoi.dashboard.auth import issue_dashboard_jwt
from momoi.dashboard.settings import DashboardSettings
from momoi.runtime.supervisor import RuntimeSupervisor
from momoi.storage import Store


SCHEMA = {
    "tenant": {"type": "string", "label": "租户", "required": True},
    "region": {"type": "string", "enum": ["east", "west"], "default": "east"},
    "auth": {
        "type": "object",
        "required": True,
        "properties": {
            "client": {"type": "string", "required": True, "secret": True},
            "scope": {"type": "string", "default": "read"},
        },
    },
    "accounts": {
        "type": "array",
        "default": [],
        "items": {
            "type": "object",
            "properties": {
                "credential": {"type": "string", "secret": True, "required": True},
            },
        },
    },
    "retries": {
        "type": "integer",
        "minimum": 0,
        "maximum": 5,
        "default": 2,
        "advanced": True,
        "description": "请求失败后的重试次数",
    },
    "sandbox": {"type": "boolean", "default": False},
}


class ProviderFieldsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.json"
        bootstrap(self.path)
        self.manager = ConfigurationManager(self.path)
        self.name = "fields_" + uuid.uuid4().hex
        self.instances = []

        class Balance:
            def __init__(instance, options, context):
                instance.options = options
                instance.closed = False
                self.instances.append(instance)

            async def balance(instance):
                return {
                    "source": "live",
                    "currency": "CNY",
                    "is_available": True,
                    "total_balance": "12.34",
                }

            async def close(instance):
                instance.closed = True

        register_adapter(
            Adapter(self.name, "balance", Balance, lambda options: None, SCHEMA)
        )
        self.document = {
            "adapter": self.name,
            "enabled": True,
            "options": {
                "tenant": "demo",
                "auth": {"client": "nested-private"},
                "accounts": [{"credential": "array-private"}],
            },
        }

    def save(self, document):
        return self.manager.save_binding("balance", document, self.manager.revision())

    async def test_nested_form_contract_defaults_secrets_and_factory(self):
        saved = self.save(self.document)
        encoded = json.dumps(saved)
        self.assertIn("nested-private", encoded)
        self.assertIn("array-private", encoded)
        declared = next(
            item["fields"] for item in saved["adapters"] if item["adapter"] == self.name
        )
        self.assertEqual(declared, SCHEMA)
        editable = saved["capabilities"]["balance"]
        self.assertEqual(editable["options"]["auth"]["client"], "nested-private")
        editable["options"]["tenant"] = "changed"
        self.save(editable)
        registry = ServiceRegistry(self.manager.validate().providers)
        balance = registry.balance
        self.assertEqual(balance.options["region"], "east")
        self.assertEqual(
            balance.options["auth"], {"client": "nested-private", "scope": "read"}
        )
        self.assertEqual(balance.options["retries"], 2)
        self.assertIs(balance.options["sandbox"], False)
        async with registry:
            self.assertEqual((await balance.balance())["total_balance"], "12.34")
        self.assertTrue(balance.closed)
        # Raw document edits preserve literal nested credentials.
        saved = self.manager.snapshot()
        self.manager.save("providers", saved["providers"], saved["revision"])
        self.assertEqual(
            self.manager.validate().providers.options_for("balance")["accounts"][0][
                "credential"
            ],
            "array-private",
        )

    def test_invalid_values_do_not_write_or_create_resources(self):
        for key, value in [
            ("tenant", ""),
            ("region", "north"),
            ("retries", True),
            ("retries", 6),
            ("sandbox", "false"),
            ("auth", {}),
            ("accounts", [{"credential": 123}]),
            ("unknown", "oops"),
        ]:
            with self.subTest(field=key, value=value):
                invalid = copy.deepcopy(self.document)
                invalid["options"][key] = value
                revision = self.manager.revision()
                with self.assertRaises(ConfigError):
                    self.save(invalid)
                self.assertEqual(self.manager.revision(), revision)
        self.assertEqual(self.instances, [])

    def test_nested_environment_secret_and_disabled_incomplete_binding(self):
        self.document["options"]["auth"]["client"] = {"env": "CUSTOM_PROVIDER_AUTH"}
        with patch.dict("os.environ", {"CUSTOM_PROVIDER_AUTH": "env-private"}):
            saved = self.save(self.document)
            self.assertNotIn("env-private", json.dumps(saved))
            self.assertNotIn("env-private", self.manager.provider_path.read_text())
            self.assertEqual(
                self.manager.validate().providers.options_for("balance")["auth"][
                    "client"
                ],
                "env-private",
            )
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ConfigError):
                self.save(self.document)
            self.document["enabled"] = False
            self.save(self.document)
            registry = ServiceRegistry(self.manager.validate().providers)
            self.assertIsNone(registry.balance)
        self.document["options"] = {}
        self.save(self.document)
        self.assertEqual(self.instances, [])

    async def test_switching_adapters_uses_same_consumer_and_closes_instances(self):
        self.manager.save_binding(
            "llm",
            {
                "adapter": "openai",
                "options": {"base_url": "http://localhost", "model": "test"},
            },
            self.manager.revision(),
        )
        self.manager.save_runtime(
            {
                "channels": {
                    "primary": "napcat",
                    "enabled": {"napcat": {"url": "ws://localhost", "owner_qq": "123"}},
                }
            },
            self.manager.revision(),
        )

        class Runtime:
            def __init__(self, config):
                self.services = ServiceRegistry(config.providers)
                self.ready = asyncio.Event()

            async def run(self, stop):
                async with self.services:
                    self.ready.set()
                    await stop.wait()

        supervisor = RuntimeSupervisor(self.manager, factory=Runtime)
        self.addAsyncCleanup(supervisor._retire)
        self.save(self.document)
        await supervisor.apply()
        self.assertEqual(supervisor.state, "running")
        service = supervisor.daemon.services.balance
        result = await supervisor.balance()

        class Replacement:
            closed = False

            async def balance(self):
                return result

            async def close(self):
                self.closed = True

        name = "replacement_" + uuid.uuid4().hex
        register_adapter(
            Adapter(
                name,
                "balance",
                lambda options, ctx: Replacement(),
                lambda options: None,
                {"project": {"type": "integer", "required": True}},
            )
        )
        self.save({"adapter": name, "enabled": True, "options": {"project": 42}})
        await supervisor.apply()
        self.assertEqual(supervisor.state, "running")
        self.assertTrue(service.closed)
        replacement = supervisor.daemon.services.balance
        self.assertEqual(await supervisor.balance(), result)
        await supervisor._retire()
        self.assertTrue(replacement.closed)

    async def test_http_contract_for_custom_fields_and_validation(self):
        config = self.manager.dashboard_config()
        store = Store(config.database, config.workspace)
        self.addCleanup(store.close)
        runtime = RuntimeSupervisor(self.manager)
        client = TestClient(
            TestServer(
                create_dashboard_app(
                    store,
                    token=config.dashboard.token,
                    configuration=self.manager,
                    runtime=runtime,
                    settings=DashboardSettings.from_config(config),
                )
            )
        )
        await client.start_server()
        self.addAsyncCleanup(client.close)
        client.session.headers["Authorization"] = "Bearer " + issue_dashboard_jwt(
            config.dashboard.token
        )
        response = await client.get("/api/settings/configuration")
        self.assertEqual(response.status, 200)
        snapshot = await response.json()
        self.assertEqual(
            next(
                item["fields"]
                for item in snapshot["adapters"]
                if item["adapter"] == self.name
            ),
            SCHEMA,
        )
        response = await client.put(
            "/api/settings/providers/balance",
            json={
                "revision": snapshot["revision"],
                "document": self.document,
            },
        )
        self.assertEqual(response.status, 200)
        snapshot = await response.json()
        self.assertIn("nested-private", json.dumps(snapshot))
        self.assertTrue(runtime.changed.is_set())
        document = snapshot["capabilities"]["balance"]
        document["options"]["retries"] = -1
        response = await client.put(
            "/api/settings/providers/balance",
            json={
                "revision": snapshot["revision"],
                "document": document,
            },
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.manager.revision(), snapshot["revision"])

    def test_provider_placeholders_are_rejected_and_schema_names_are_scoped(self):
        saved = self.save(self.document)
        name = "other_" + uuid.uuid4().hex
        register_adapter(
            Adapter(
                name, "balance", lambda options, ctx: None, lambda options: None, SCHEMA
            )
        )
        changed = saved["capabilities"]["balance"]
        changed["adapter"] = name
        changed["options"]["auth"]["client"] = KEEP_SECRET
        with self.assertRaisesRegex(ConfigError, "must be string"):
            self.save(changed)
        # A field named client is secret only at the declared auth.client path.
        from momoi.integrations.fields import redact_fields

        fields = {"client": {"type": "string"}, "auth": SCHEMA["auth"]}
        self.assertEqual(
            redact_fields(fields, {"client": "public", "auth": {"client": "private"}}),
            {"client": "public", "auth": {"client": KEEP_SECRET}},
        )

    def test_schema_validation_and_default_isolation(self):
        for schema in [
            {"x": {"type": "file"}},
            {"x": {"type": "array"}},
            {"x": {"type": "object", "secret": True}},
            {"x": {"type": "integer", "default": "bad"}},
            {"x": {"type": "string", "required": "yes"}},
            {"x": {"type": "integer", "minimum": "0"}},
            {"x": {"type": "string", "secret": True, "default": "private"}},
            {
                "x": {
                    "type": "object",
                    "properties": SCHEMA["auth"]["properties"],
                    "default": {"client": "private"},
                }
            },
        ]:
            with self.assertRaises(ValueError):
                validate_schema(schema)
        result = normalize_fields(SCHEMA, self.document["options"])
        result["accounts"][0]["credential"] = "changed"
        self.assertEqual(
            self.document["options"]["accounts"][0]["credential"], "array-private"
        )
