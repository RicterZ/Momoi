import copy
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from momoi.config.loading import load_config
from momoi.config.models import ConfigError
from momoi.dashboard.app import create_dashboard_app
from momoi.dashboard.auth import issue_dashboard_jwt
from momoi.dashboard.settings import DashboardSettings
from momoi.integrations.adapters.deepseek import (
    DeepSeekAccounting,
    DeepSeekBalanceProvider,
)
from momoi.integrations.adapters.fish import FishAudioTTSProvider
from momoi.integrations.adapters.tencent import TencentASRProvider
from momoi.integrations.configuration import load_provider_catalog
from momoi.integrations.contracts.tts import AudioOutput
from momoi.integrations.errors import ErrorCategory, IntegrationError
from momoi.integrations.registry import ServiceRegistry, register_adapter
from momoi.storage import Store
from tests.support import write_app_config


def catalog_data():
    return {
        "version": 1,
        "credentials": {"shared": {"api_key": {"env": "TEST_MODEL_KEY"}}},
        "services": {
            "chat": {
                "adapter": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "credentials": "shared",
            }
        },
        "bindings": {
            "llm": {"service": "chat", "options": {"model": "deepseek-v4-flash"}},
            "balance": {"service": "chat"},
        },
    }


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "providers.yaml"
        self.env = patch.dict("os.environ", {"TEST_MODEL_KEY": "private-secret"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def load(self, raw):
        self.path.write_text(yaml.safe_dump(raw))
        return load_provider_catalog(self.path.resolve())

    def test_shared_credentials_and_independent_capabilities(self):
        catalog = self.load(catalog_data())
        services = ServiceRegistry(catalog)
        self.assertEqual(services.llm.config.api_key, services.balance.api_key)
        self.assertEqual(services.balance.base_url, "https://api.deepseek.com")
        self.assertIsInstance(services.llm.accounting, DeepSeekAccounting)
        self.assertFalse(hasattr(services.llm.accounting, "balance"))
        self.assertIsNone(services.asr)
        self.assertIsNone(services.tts)
        self.assertNotIn("private-secret", repr(catalog))
        self.assertNotIn("private-secret", repr(services.llm.config))
        options = catalog.options_for("llm")
        options["model"] = "changed"
        self.assertEqual(catalog.options_for("llm")["model"], "deepseek-v4-flash")
        raw = catalog_data()
        raw["services"]["other"] = {
            "adapter": "openai",
            "base_url": "http://localhost",
            "settings": {"model": "local"},
        }
        raw["bindings"]["llm"] = {"service": "other"}
        services = ServiceRegistry(self.load(raw))
        self.assertIsNone(services.llm.accounting)
        self.assertIsInstance(services.balance, DeepSeekBalanceProvider)

    def test_all_five_bindings_and_option_precedence(self):
        raw = catalog_data()
        raw["services"].update(
            {
                "speech": {
                    "adapter": "fish",
                    "credentials": "shared",
                    "settings": {"model": "s1", "reference_id": "voice"},
                },
                "recognition": {
                    "adapter": "tencent",
                    "settings": {"secret_id": "id", "secret_key": "key"},
                },
                "vectors": {
                    "adapter": "openai",
                    "base_url": "http://localhost:8002/v1",
                    "timeout_seconds": 12,
                },
            }
        )
        raw["bindings"].update(
            {
                "tts": {"service": "speech", "options": {"model": "s2.1-pro-free"}},
                "asr": {"service": "recognition", "options": {"max_audio_bytes": 1024}},
                "embedding": {
                    "service": "vectors",
                    "options": {"document_batch_size": 4},
                },
            }
        )
        services = ServiceRegistry(self.load(raw))
        self.assertIsInstance(services.tts, FishAudioTTSProvider)
        self.assertEqual(services.tts.model, "s2.1-pro-free")
        self.assertIsInstance(services.asr, TencentASRProvider)
        self.assertEqual(
            services.embedding.config.endpoint, "http://localhost:8002/v1/embeddings"
        )
        self.assertEqual(services.embedding_config.document_batch_size, 4)
        self.assertEqual(services.embedding.config.query_timeout_seconds, 12)

    def test_main_config_only_references_relative_catalog(self):
        raw = catalog_data()
        self.load(raw)
        (self.root / "prompts").mkdir()
        (self.root / "prompts/SOUL.md").write_text("soul")
        main = {
            "providers": "./providers.yaml",
            "channels": {"primary": "weixin", "enabled": {"weixin": {}}},
            "context": {},
            "storage": {"database": "data/store.sqlite3"},
            "logging": {},
        }
        path = self.root / "config.json"
        write_app_config(path, main)
        config = load_config(path)
        self.assertEqual(config.providers.path, self.path.resolve())
        self.assertFalse(hasattr(config, "llm"))
        self.assertFalse(hasattr(config, "usage"))
        self.assertEqual(
            config.providers.options_for("llm")["api_key"], "private-secret"
        )

    def test_thinking_and_numeric_options_validated_before_startup(self):
        raw = catalog_data()
        raw["bindings"]["llm"]["options"]["thinking"] = {
            "effort": "high",
            "stages": {"reply_followup": "low"},
        }
        catalog = self.load(raw)
        thinking = ServiceRegistry(catalog).llm.config.thinking
        self.assertEqual(thinking.for_stage("owner"), "high")
        self.assertEqual(thinking.for_stage("reply_followup"), "low")
        for field, bad in [
            ("max_retries", -1),
            ("timeout_seconds", float("nan")),
            ("max_tokens", True),
            ("tool_choice", "false"),
            ("thinking", {"effort": "medium"}),
            ("unknown", "value"),
        ]:
            with self.subTest(field=field):
                invalid = copy.deepcopy(raw)
                invalid["bindings"]["llm"]["options"][field] = bad
                with self.assertRaises(ConfigError):
                    self.load(invalid)

    def test_reference_schema_and_duplicate_validation(self):
        cases = []
        for key, value in [
            ("version", 2),
            ("plugins", "module"),
            ("bindings", []),
            ("typo", {}),
        ]:
            cases.append({**catalog_data(), key: value})
        for binding in [
            {"service": "missing"},
            {"service": "chat", "enabled": "true"},
            {"service": "chat", "options": []},
            {"service": "chat", "extra": 1},
            {"service": "chat", "enabled": False},
        ]:
            raw = catalog_data()
            raw["bindings"]["llm"] = binding
            cases.append(raw)
        raw = catalog_data()
        raw["services"]["chat"]["credentials"] = "missing"
        cases.append(raw)
        raw = catalog_data()
        raw["services"]["chat"]["adapter"] = "fish"
        cases.append(raw)
        raw = catalog_data()
        raw["bindings"]["llm"]["options"]["api_key"] = "duplicate"
        cases.append(raw)
        for raw in cases:
            with self.subTest(raw=repr(raw)):
                with self.assertRaises(ConfigError):
                    self.load(raw)
        for content in [
            "version: 1\nversion: 1\n",
            "version: 1\nservices: [private-secret",
            "true: 1\n",
        ]:
            self.path.write_text(content)
            with self.assertRaises(ConfigError) as caught:
                load_provider_catalog(self.path.resolve())
            self.assertNotIn("private-secret", str(caught.exception))

    def test_disabled_credentials_need_not_exist_and_env_is_explicit(self):
        raw = catalog_data()
        raw["credentials"]["optional"] = {"api_key": {"env": "MOMOI_TEST_ABSENT"}}
        raw["services"]["voice"] = {"adapter": "fish", "credentials": "optional"}
        raw["bindings"]["tts"] = {"service": "voice", "enabled": False}
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ConfigError, "TEST_MODEL_KEY"):
                self.load(raw)
            raw["credentials"]["shared"]["api_key"] = "literal-key"
            catalog = self.load(raw)
            self.assertFalse(catalog.enabled("tts"))
            raw["bindings"]["tts"]["enabled"] = True
            with self.assertRaisesRegex(ConfigError, "MOMOI_TEST_ABSENT"):
                self.load(raw)


class IntegrationLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def load(self, raw):
        path = self.root / "providers.yaml"
        path.write_text(yaml.safe_dump(raw))
        return load_provider_catalog(path)

    async def test_custom_plugin_factory_without_core_changes(self):
        name = "test_" + uuid.uuid4().hex
        module_path = self.root / f"{name}.py"
        module_path.write_text("""from momoi.integrations.registry import register_adapter
from momoi.integrations.contracts.tts import AudioOutput
class Voice:
    def __init__(self, options, context):
        self.prefix = options['prefix']
        self.transport = context.transport
        self.closed = False
    async def synthesize(self, text):
        return AudioOutput((self.prefix + text).encode(), 'mp3')
    async def close(self):
        self.closed = True
def validate(options):
    if not isinstance(options.get('prefix'), str):
        raise ValueError('prefix required')
register_adapter(__name__, 'tts', Voice, validate=validate)
""")
        raw = catalog_data()
        raw["credentials"]["shared"]["api_key"] = "key"
        raw["plugins"] = [name]
        raw["services"]["voice"] = {"adapter": name, "settings": {"prefix": "voice:"}}
        raw["bindings"]["tts"] = {"service": "voice"}
        with patch("sys.path", [str(self.root), *__import__("sys").path]):
            catalog = self.load(raw)
        services = ServiceRegistry(catalog)
        voice = services.tts
        async with services:
            self.assertEqual((await voice.synthesize("hello")).data, b"voice:hello")
            self.assertIs(voice.transport, services.transport)
            self.assertEqual(set(services._instances), {"tts"})
        self.assertTrue(voice.closed)
        self.assertIsNone(services.transport._session)

    async def test_registry_closes_on_failure_and_does_not_close_injected_services(
        self,
    ):
        closed = []
        name = "test_" + uuid.uuid4().hex

        class Balance:
            async def balance(self):
                return {}

            async def close(self):
                closed.append("balance")

        class Voice:
            async def synthesize(self, text):
                return AudioOutput(b"voice", "mp3")

            async def __aenter__(self):
                raise RuntimeError("startup failed")

            async def __aexit__(self, *exc):
                pass

        register_adapter(
            name,
            "balance",
            lambda options, ctx: Balance(),
            validate=lambda options: None,
        )
        register_adapter(
            name, "tts", lambda options, ctx: Voice(), validate=lambda options: None
        )
        raw = catalog_data()
        raw["credentials"]["shared"]["api_key"] = "key"
        raw["services"]["custom"] = {"adapter": name}
        raw["bindings"]["balance"] = {"service": "custom"}
        raw["bindings"]["tts"] = {"service": "custom"}
        services = ServiceRegistry(self.load(raw))
        services.balance
        services.tts
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            async with services:
                pass
        self.assertEqual(closed, ["balance"])
        self.assertIsNone(services.transport._session)
        services = ServiceRegistry(self.load(raw), overrides={"balance": Balance()})
        services.balance
        async with services:
            pass
        self.assertEqual(closed, ["balance"])

    async def test_balance_http_errors_are_typed_and_dashboard_isolated(self):
        mode = {
            "status": 200,
            "payload": {
                "is_available": True,
                "balance_infos": [{"currency": "CNY", "total_balance": "12.34"}],
            },
        }
        seen = []

        async def balance(request):
            seen.append(request.headers["Authorization"])
            return web.json_response(mode["payload"], status=mode["status"])

        app = web.Application()
        app.router.add_get("/user/balance", balance)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        raw = catalog_data()
        raw["credentials"]["shared"]["api_key"] = "shared-key"
        raw["services"]["chat"]["base_url"] = str(server.make_url("/v1"))
        services = ServiceRegistry(self.load(raw))
        provider = services.balance
        store = Store(self.root / "store.sqlite3")
        self.addCleanup(store.close)
        client = TestClient(
            TestServer(
                create_dashboard_app(
                    store,
                    token="test-secret",
                    balance_provider=provider,
                    settings=DashboardSettings(()),
                )
            )
        )
        await client.start_server()
        self.addAsyncCleanup(client.close)
        client.session.headers["Authorization"] = "Bearer " + issue_dashboard_jwt(
            "test-secret"
        )
        async with services:
            first_session = services.transport._session
            self.assertEqual((await provider.balance())["total_balance"], "12.34")
            self.assertIs(first_session, services.transport._session)
            self.assertEqual(seen, ["Bearer shared-key"])
            mode["status"] = 401
            with self.assertRaises(IntegrationError) as caught:
                await provider.balance()
            self.assertEqual(caught.exception.category, ErrorCategory.AUTHENTICATION)
            self.assertNotIn("shared-key", str(caught.exception))
            response = await client.get("/api/overview")
            self.assertEqual(response.status, 200)
            self.assertEqual(
                (await response.json())["balance"]["source"], "unavailable"
            )
            mode["status"] = 200
            mode["payload"] = {}
            with self.assertRaises(IntegrationError) as caught:
                await provider.balance()
            self.assertEqual(caught.exception.category, ErrorCategory.INVALID_RESPONSE)
        self.assertTrue(first_session.closed)

    async def test_dashboard_exposes_only_prompt_settings(self):
        from momoi.dashboard.settings import DashboardSettings, PromptFile

        store = Store(self.root / "store.sqlite3")
        self.addCleanup(store.close)
        prompt = self.root / "SOUL.md"
        prompt.write_text("soul")
        client = TestClient(
            TestServer(
                create_dashboard_app(
                    store,
                    token="test-secret",
                    settings=DashboardSettings((PromptFile("soul", prompt, True),)),
                )
            )
        )
        await client.start_server()
        self.addAsyncCleanup(client.close)
        client.session.headers["Authorization"] = "Bearer " + issue_dashboard_jwt(
            "test-secret"
        )
        self.assertEqual(
            set(await (await client.get("/api/settings")).json()), {"prompts"}
        )
        for method in ["get", "put"]:
            response = await getattr(client, method)("/api/settings/llm")
            self.assertEqual(response.status, 404)

    async def test_custom_llm_asr_and_embedding_use_capability_contracts(self):
        from momoi.integrations.contracts.asr import AudioInput
        from momoi.integrations.validation import llm_config
        from momoi.models import ProviderResponse

        name = "test_" + uuid.uuid4().hex

        class Model:
            def __init__(self, options, context):
                self.config = llm_config(
                    {"base_url": "http://localhost", "model": "custom"}, "openai"
                )
                self.accounting = self.usage_sink = self.thinking_sink = (
                    self.usage_parser
                ) = None

            async def complete(self, *args, **kwargs):
                return ProviderResponse([{"type": "text", "text": "custom"}], [])

        class Recognition:
            async def transcribe(self, audio):
                return audio.data.decode()

        class Vectors:
            def __init__(self, options, context):
                self.endpoint = options["base_url"]
                self.closed = False

            async def encode(self, texts, *, query):
                return [[1.0, 0.0] for _ in texts]

            async def health(self):
                return True, 0, ""

            async def close(self):
                self.closed = True

        register_adapter(name, "llm", Model, validate=lambda options: None)
        register_adapter(
            name,
            "asr",
            lambda options, ctx: Recognition(),
            validate=lambda options: None,
        )
        register_adapter(name, "embedding", Vectors, validate=lambda options: None)
        raw = {
            "version": 1,
            "services": {"custom": {"adapter": name}},
            "bindings": {
                "llm": {"service": "custom"},
                "asr": {"service": "custom"},
                "embedding": {
                    "service": "custom",
                    "options": {
                        "base_url": "grpc://encoder",
                        "model": "custom-vector",
                        "dimensions": 2,
                    },
                },
            },
        }
        services = ServiceRegistry(self.load(raw))
        model, asr, vectors = services.llm, services.asr, services.embedding
        self.assertEqual(services.embedding_config.model, "custom-vector")
        self.assertFalse(hasattr(services.embedding_config, "api_key"))
        self.assertFalse(hasattr(services.embedding_config, "endpoint"))
        async with services:
            self.assertEqual(
                (await model.complete("system", [])).content[0]["text"], "custom"
            )
            self.assertEqual(await asr.transcribe(AudioInput(b"hello", "mp3")), "hello")
            self.assertEqual(await vectors.encode(["hello"], query=True), [[1.0, 0.0]])
        self.assertTrue(vectors.closed)
