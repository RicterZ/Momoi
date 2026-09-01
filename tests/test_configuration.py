import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import asyncio


from momoi.__main__ import emotion as emotion_command, goal as goal_command, parse_args
from momoi.agenda_tools import AgendaTools
from momoi.channel.napcat import NapCatConfig
from momoi.channel.weixin import WeixinConfig
from momoi.config import (
    ASRConfig,
    ConfigError,
    DashboardConfig,
    EmbeddingConfig,
    NotificationConfig,
    ThinkingConfig,
    UsageConfig,
    load_config,
)
from momoi.mcp_client import load_mcp_servers
from momoi.models import (
    ToolCall,
    TurnDraft,
)
from momoi.storage import Store


def _napcat_channels() -> dict[str, object]:
    return {
        "primary": "napcat",
        "enabled": {
            "napcat": {"url": "ws://localhost", "owner_qq": "123"},
        },
    }


class ConfigurationTest(unittest.TestCase):
    def test_loads_and_validates_embedding_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            path = root / "config.json"
            value = {
                "llm": {
                    "base_url": "https://example.com",
                    "api_key": "key",
                    "model": "model",
                },
                "channels": _napcat_channels(),
                "context": {},
                "storage": {"database": "momoi.sqlite3"},
                "logging": {},
            }
            path.write_text(json.dumps(value))
            default_embedding = load_config(path).embedding
            self.assertEqual(default_embedding, EmbeddingConfig())
            self.assertEqual(default_embedding.query_timeout_seconds, 5)

            value["embedding"] = {
                "enabled": True,
                "endpoint": "http://localhost:8002/v1/embeddings/",
                "model": "BAAI/bge-small-zh-v1.5",
                "dimensions": 512,
                "calibration_profile": "bge-small-zh-v1.5-momoi-v1",
                "query_timeout_seconds": 0.25,
                "document_timeout_seconds": 10,
                "document_batch_size": 4,
            }
            path.write_text(json.dumps(value))
            config = load_config(path)
            self.assertTrue(config.embedding.enabled)
            self.assertEqual(
                config.embedding.endpoint,
                "http://localhost:8002/v1/embeddings",
            )
            self.assertEqual(config.embedding.document_batch_size, 4)

            value["embedding"]["dimensions"] = 0  # type: ignore[index]
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ConfigError, "dimensions must be positive"):
                load_config(path)

    def test_loads_stage_specific_thinking_effort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            path = root / "config.json"
            value = {
                "llm": {
                    "base_url": "https://example.com",
                    "api_key": "key",
                    "model": "model",
                    "thinking": {
                        "effort": "high",
                        "stages": {
                            "heartbeat": "low",
                            "reply_followup": "low",
                        },
                    },
                },
                "channels": _napcat_channels(),
                "context": {},
                "storage": {"database": "momoi.sqlite3"},
                "logging": {},
            }
            path.write_text(json.dumps(value))

            config = load_config(path)

            self.assertEqual(
                config.llm.thinking,
                ThinkingConfig(
                    effort="high",
                    stages={
                        "heartbeat": "low",
                        "reply_followup": "low",
                    },
                ),
            )
            self.assertEqual(config.llm.thinking.for_stage("owner"), "high")
            self.assertEqual(
                config.llm.thinking.for_stage("heartbeat"),
                "low",
            )

            value["llm"]["thinking"]["effort"] = "medium"  # type: ignore[index]
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ConfigError, "must be low, high, or max"):
                load_config(path)

    def test_loads_and_validates_asr_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            path = root / "config.json"
            value = {
                "llm": {
                    "base_url": "https://example.com",
                    "api_key": "key",
                    "model": "model",
                },
                "channels": _napcat_channels(),
                "context": {},
                "storage": {"database": "momoi.sqlite3"},
                "logging": {},
            }
            path.write_text(json.dumps(value))
            config = load_config(path)
            self.assertEqual(config.asr, ASRConfig())

            value["asr"] = {
                "enabled": True,
                "provider": "tencent",
                "timeout_seconds": 12,
                "max_audio_bytes": 1024,
                "settings": {
                    "secret_id": "id",
                    "secret_key": "secret",
                    "region": "ap-shanghai",
                },
            }
            path.write_text(json.dumps(value))
            config = load_config(path)
            self.assertTrue(config.asr.enabled)
            self.assertEqual(config.asr.timeout_seconds, 12)
            self.assertEqual(config.asr.max_audio_bytes, 1024)
            self.assertEqual(config.asr.settings["region"], "ap-shanghai")  # type: ignore[index]

            value["asr"]["settings"]["secret_key"] = ""  # type: ignore[index]
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ConfigError, "secret_key is required"):
                load_config(path)

    def test_loads_multiple_channels_and_validates_primary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            path = root / "config.json"
            value = {
                "llm": {
                    "base_url": "https://example.com",
                    "api_key": "key",
                    "model": "model",
                },
                "channels": {
                    "primary": "napcat",
                    "enabled": {
                        "napcat": {"url": "ws://localhost", "owner_qq": "123"},
                        "weixin": {},
                    },
                },
                "context": {},
                "storage": {"database": "momoi.sqlite3"},
                "logging": {},
            }
            path.write_text(json.dumps(value))
            config = load_config(path)
            self.assertIsInstance(config.channel, NapCatConfig)
            self.assertTrue(config.llm.tool_choice)
            self.assertEqual(config.llm.max_tokens, 16384)
            self.assertEqual(config.llm.timeout_seconds, 300)
            self.assertEqual(config.summary_results, 8)
            self.assertEqual(config.transcript_turns_min, 48)
            self.assertEqual(config.transcript_turns_max, 96)
            self.assertEqual(config.episode_raw_tail_turns, 6)
            self.assertEqual(config.max_input_tokens, 142222)
            self.assertEqual(config.context_compaction_ratio, 0.9)
            self.assertTrue(config.episode_annealing.enabled)
            self.assertEqual(config.episode_annealing.idle_seconds, 60)
            self.assertEqual(config.episode_annealing.max_seconds, 650)
            self.assertEqual(
                [type(item) for item in config.channel_configs],
                [NapCatConfig, WeixinConfig],
            )

            value["llm"]["tool_choice"] = False  # type: ignore[index]
            path.write_text(json.dumps(value))
            configured = load_config(path)
            self.assertFalse(configured.llm.tool_choice)

            value["channels"]["primary"] = "missing"  # type: ignore[index]
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ConfigError, "must name an enabled channel"):
                load_config(path)

    def test_context_result_limits_are_capped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            path = root / "config.json"
            value = {
                "llm": {
                    "base_url": "https://example.com",
                    "api_key": "key",
                    "model": "model",
                },
                "channels": _napcat_channels(),
                "context": {},
                "storage": {"database": "momoi.sqlite3"},
                "logging": {},
            }
            path.write_text(json.dumps(value))
            value["context"]["summary_results"] = 99  # type: ignore[index]
            value["context"]["memory_results"] = 99  # type: ignore[index]
            path.write_text(json.dumps(value))
            configured = load_config(path)
            self.assertEqual(configured.summary_results, 12)
            self.assertEqual(configured.memory_results, 6)

    def test_loads_primary_channel_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            (root / "prompts" / "HEARTBEAT.md").write_text("偶尔整理自己的摄影兴趣。")
            path = root / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "llm": {
                            "base_url": "https://example.com",
                            "api_key": "key",
                            "model": "model",
                        },
                        "channels": _napcat_channels(),
                        "context": {},
                        "tools": {"result_retention_days": 14},
                        "autonomy": {
                            "allowed_tools": [
                                "curl",
                                "mcp__brave-search__brave_web_search",
                            ]
                        },
                        "storage": {"database": "momoi.sqlite3"},
                        "logging": {},
                    }
                )
            )
            config = load_config(path)
            self.assertIsInstance(config.channel, NapCatConfig)
            self.assertEqual(config.channel.owner_qq, "123")
            self.assertFalse(config.reflection.enabled)
            self.assertEqual(config.reflection.at, "03:00")
            self.assertEqual(
                config.autonomy.allowed_tools,
                ("curl", "mcp__brave-search__brave_web_search"),
            )
            self.assertEqual(config.heartbeat_prompt, "偶尔整理自己的摄影兴趣。")
            self.assertEqual(
                config.heartbeat_prompt_path,
                (root / "prompts" / "HEARTBEAT.md").resolve(),
            )
            self.assertEqual(config.heartbeat.max_interval_seconds, 5400)
            self.assertFalse(hasattr(config.heartbeat, "reply_initial_interval_seconds"))
            self.assertFalse(hasattr(config.heartbeat, "reply_followup_interval_seconds"))
            self.assertEqual(config.dashboard.token, "")
            self.assertEqual(config.tool_result_max_chars, 12000)
            self.assertEqual(config.tool_result_retention_days, 14)

            (root / "prompts" / "HEARTBEAT.md").unlink()
            self.assertEqual(load_config(path).heartbeat_prompt, "")

            legacy = json.loads(path.read_text())
            legacy["napcat"] = legacy.pop("channels")["enabled"]["napcat"]
            path.write_text(json.dumps(legacy))
            with self.assertRaisesRegex(ConfigError, "channels must be a table/object"):
                load_config(path)

    def test_loads_dashboard_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            path = root / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "llm": {
                            "base_url": "https://example.com",
                            "api_key": "key",
                            "model": "model",
                        },
                        "channels": _napcat_channels(),
                        "dashboard": {"token": "dash-secret"},
                        "context": {},
                        "storage": {"database": "momoi.sqlite3"},
                        "logging": {},
                    }
                )
            )
            config = load_config(path)
            self.assertIsInstance(config.dashboard, DashboardConfig)
            self.assertEqual(config.dashboard.token, "dash-secret")
            self.assertEqual(config.usage.provider, "")
            self.assertEqual(config.usage.api_key, "")

    def test_loads_usage_plugin_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            path = root / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "llm": {
                            "base_url": "https://example.com",
                            "api_key": "key",
                            "model": "model",
                        },
                        "channels": _napcat_channels(),
                        "usage": {
                            "provider": "momoi.extensions.deepseek.DeepSeekPlugin",
                            "api_key": "sk-usage",
                            "base_url": "https://api.deepseek.com",
                            "timeout_seconds": 8,
                        },
                        "context": {},
                        "storage": {"database": "momoi.sqlite3"},
                        "logging": {},
                    }
                )
            )
            config = load_config(path)
            self.assertIsInstance(config.usage, UsageConfig)
            self.assertEqual(
                config.usage.provider,
                "momoi.extensions.deepseek.DeepSeekPlugin",
            )
            self.assertEqual(config.usage.api_key, "sk-usage")
            self.assertEqual(
                config.usage.settings,
                {
                    "base_url": "https://api.deepseek.com",
                    "timeout_seconds": 8,
                },
            )

    def test_environment_overrides_docker_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            path = root / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "llm": {
                            "base_url": "https://example.com",
                            "api_key": "key",
                            "model": "model",
                        },
                        "channels": {
                            "primary": "napcat",
                            "enabled": {
                                "napcat": {
                                    "url": "ws://localhost",
                                    "owner_qq": "123",
                                },
                                "weixin": {},
                            },
                        },
                        "dashboard": {"token": "dash-secret"},
                        "webhooks": {
                            "enabled": False,
                            "host": "127.0.0.1",
                            "token": "old-hook",
                        },
                        "notifications": {"timezone": "UTC"},
                        "context": {},
                        "storage": {"database": "momoi.sqlite3"},
                        "logging": {},
                    }
                )
            )
            with patch.dict(
                "os.environ",
                {
                    "MOMOI_LLM_API_FORMAT": "openai",
                    "MOMOI_LLM_BASE_URL": "https://llm.example",
                    "MOMOI_LLM_API_KEY": "sk-from-env",
                    "MOMOI_LLM_MODEL": "env-model",
                    "MOMOI_NAPCAT_URL": "ws://napcat:3001",
                    "MOMOI_OWNER_QQ": "999",
                    "MOMOI_PRIMARY": "weixin",
                    "MOMOI_TIMEZONE": "Asia/Shanghai",
                    "MOMOI_DASHBOARD_TOKEN": "env-dash",
                    "MOMOI_WEBHOOKS_ENABLED": "true",
                    "MOMOI_WEBHOOKS_HOST": "0.0.0.0",
                    "MOMOI_WEBHOOKS_TOKEN": "env-hook",
                    "MOMOI_ASR_SECRET_ID": "env-asr-id",
                    "MOMOI_ASR_SECRET_KEY": "env-asr-key",
                },
                clear=False,
            ):
                config = load_config(path)
            self.assertEqual(config.llm.api_format, "openai")
            self.assertEqual(config.llm.base_url, "https://llm.example")
            self.assertEqual(config.llm.api_key, "sk-from-env")
            self.assertEqual(config.llm.model, "env-model")
            self.assertEqual(config.channel.plugin, "weixin")
            napcat = next(
                item for item in config.channel_configs if item.plugin == "napcat"
            )
            self.assertEqual(napcat.url, "ws://napcat:3001")
            self.assertEqual(napcat.owner_qq, "999")
            self.assertEqual(config.notifications.timezone, "Asia/Shanghai")
            self.assertEqual(config.dashboard.token, "env-dash")
            self.assertTrue(config.webhooks.enabled)
            self.assertEqual(config.webhooks.host, "0.0.0.0")
            self.assertEqual(config.webhooks.token, "env-hook")
            self.assertEqual(config.asr.settings["secret_id"], "env-asr-id")  # type: ignore[index]
            self.assertEqual(config.asr.settings["secret_key"], "env-asr-key")  # type: ignore[index]

    def test_dashboard_flag_requires_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            path = root / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "llm": {
                            "base_url": "https://example.com",
                            "api_key": "key",
                            "model": "model",
                        },
                        "channels": _napcat_channels(),
                        "context": {},
                        "storage": {"database": "momoi.sqlite3"},
                        "logging": {},
                    }
                )
            )
            with self.assertRaisesRegex(ValueError, "dashboard.token is required"):
                asyncio.run(
                    __import__("momoi.__main__", fromlist=["run"]).run(
                        path, dashboard=True
                    )
                )

    def test_config_rejects_string_booleans(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            path = root / "config.json"
            config = {
                "llm": {"base_url": "https://example.com", "api_key": "key", "model": "model"},
                "channels": _napcat_channels(),
                "context": {},
                "storage": {"database": "momoi.sqlite3"},
                "logging": {},
            }
            for section in ("webhooks", "heartbeat", "reflection"):
                config[section] = {"enabled": "false"}
                path.write_text(json.dumps(config))
                with self.assertRaisesRegex(ConfigError, rf"{section}\.enabled must be boolean"):
                    load_config(path)
                del config[section]

    def test_emotion_cli_imports_deduplicates_lists_and_deletes_managed_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            source.write_bytes(b"fake-image-content")
            database = root / "data" / "momoi.sqlite3"
            database.parent.mkdir()
            fake_config = SimpleNamespace(database=database, thinking=None)

            def run(command: str, slug: str | None = None) -> list[object]:
                arguments = SimpleNamespace(
                    emotion_command=command,
                    slug=slug,
                    path=str(source),
                    desc="开心时自然回应",
                    workspace=root,
                )
                with (
                    patch("momoi.__main__.load_config", return_value=fake_config),
                    patch("builtins.print") as output,
                ):
                    emotion_command(arguments)
                return [call.args for call in output.call_args_list]

            run("add", "happy-1")
            run("add", "happy-2")
            store = Store(database, root)
            rows = store.list_emotions()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["path"], rows[1]["path"])
            managed = Path(str(rows[0]["path"]))
            self.assertEqual(managed.parent, (root / "emotion").resolve())
            self.assertEqual(managed.suffix, ".jpg")
            self.assertTrue(managed.is_file())
            self.assertEqual(
                store._db.execute(
                    "SELECT path FROM emotions WHERE slug='happy-1'"
                ).fetchone()[0],
                f"emotion/{managed.name}",
            )
            store.close()

            listed = run("list")
            self.assertEqual(len(listed), 2)
            run("del", "happy-1")
            self.assertTrue(managed.exists())
            run("del", "happy-2")
            self.assertFalse(managed.exists())

    def test_cli_workspace_defaults_and_can_be_overridden(self) -> None:
        with patch("momoi.__main__.version", return_value="0.1.0"):
            with patch("sys.argv", ["momoi", "run"]):
                args = parse_args()
                self.assertEqual(args.workspace, Path.home() / ".momoi")
                self.assertFalse(args.dashboard)
                self.assertEqual(args.dashboard_port, 8788)
            with patch(
                "sys.argv",
                [
                    "momoi",
                    "run",
                    "--dashboard",
                    "--dashboard-host",
                    "127.0.0.1",
                    "--dashboard-port",
                    "9000",
                ],
            ):
                args = parse_args()
                self.assertTrue(args.dashboard)
                self.assertEqual(args.dashboard_host, "127.0.0.1")
                self.assertEqual(args.dashboard_port, 9000)
            with (
                tempfile.TemporaryDirectory() as directory,
                patch(
                    "sys.argv", ["momoi", "--workspace", directory, "emotion", "list"]
                ),
            ):
                self.assertEqual(parse_args().workspace, Path(directory))
            with patch("sys.argv", ["momoi", "channel", "login", "weixin"]):
                self.assertEqual(parse_args().channel_name, "weixin")

    def test_goal_cli_add_list_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "momoi.sqlite3"
            config = SimpleNamespace(
                database=database,
                thinking=None,
                notifications=NotificationConfig(timezone="Asia/Shanghai"),
            )

            def invoke(command: str, **values: object) -> list[str]:
                fields: dict[str, object] = dict(
                    workspace=root,
                    goal_command=command,
                    title="检查天气",
                    success="给出天气建议",
                    action="查询天气",
                    at=(datetime.now().astimezone() + timedelta(hours=1)).isoformat(),
                    every_seconds=None,
                    daily=None,
                    include_closed=False,
                    goal_id="",
                    reason="测试取消",
                )
                fields.update(values)
                arguments = SimpleNamespace(**fields)
                with (
                    patch("momoi.__main__.load_config", return_value=config),
                    patch("builtins.print") as output,
                ):
                    goal_command(arguments)
                return [str(call.args[0]) for call in output.call_args_list]

            added = invoke("add")
            goal_id = added[0].split("\t")[1]
            self.assertIn(goal_id, invoke("list")[0])
            deleted = invoke("del", goal_id=goal_id[:10])
            self.assertIn("\tcancelled\t", deleted[0])
            self.assertEqual(invoke("list"), [])
            all_rows = invoke("list", include_closed=True)
            self.assertIn(goal_id, all_rows[0])

    def test_goal_create_ignores_empty_unused_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            result = AgendaTools(store).execute(
                ToolCall(
                    "goal-empty-option",
                    "goal_create",
                    {
                        "title": "每日天气",
                        "success_criteria": "每天通知",
                        "next_action": "查询天气",
                        "next_review_at": "",
                        "schedule": {
                            "kind": "daily",
                            "timezone": "Asia/Shanghai",
                            "times": ["07:30"],
                        },
                    },
                ),
                TurnDraft(),
                authority="owner",
                source_event_id="test",
                allow_notify=False,
            )
            self.assertTrue(result["ok"], result)
            store.close()

    def test_loads_generic_mcp_json_and_skips_disabled_servers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.json"
            path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "search": {
                                "command": "search-server",
                                "description": "Search public sources.",
                            },
                            "off": {"command": "off-server", "disabled": True},
                        }
                    }
                )
            )
            loaded = load_mcp_servers(path)
            self.assertEqual(list(loaded), ["search"])
            self.assertEqual(
                loaded["search"]["description"],
                "Search public sources.",
            )

            value = json.loads(path.read_text())
            value["mcpServers"]["search"]["description"] = ""
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "description must be"):
                load_mcp_servers(path)
