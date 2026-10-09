from tests.support import write_app_config
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import asyncio


from momoi.__main__ import parse_args
from momoi.tools.agenda import AgendaTools
from momoi.channel.napcat import NapCatConfig
from momoi.channel.weixin import WeixinConfig
from momoi.config.loading import load_config
from momoi.config.models import ConfigError, DashboardConfig
from momoi.mcp.config import load_mcp_servers
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
    def test_loads_multiple_channels_and_validates_primary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            path = root / "config.json"
            value = {
                "providers": "providers.yaml",
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
            write_app_config(path, value)
            config = load_config(path)
            self.assertIsInstance(config.channel, NapCatConfig)
            self.assertEqual(config.summary_results, 8)
            self.assertEqual(config.transcript_turns_min, 32)
            self.assertEqual(config.transcript_turns_max, 80)
            self.assertEqual(config.episode_unsummarized_tail_turns, 6)
            self.assertEqual(config.max_input_tokens, 142222)
            self.assertEqual(config.context_compaction_ratio, 0.9)
            self.assertTrue(config.episode_annealing.enabled)
            self.assertEqual(config.episode_annealing.idle_seconds, 60)
            self.assertEqual(config.episode_annealing.max_seconds, 650)
            self.assertEqual(config.current_state.max_seconds, 180)
            self.assertEqual(
                [type(item) for item in config.channel_configs],
                [NapCatConfig, WeixinConfig],
            )

            value["context"]["episode_raw_tail_turns"] = 9
            write_app_config(path, value)
            self.assertEqual(load_config(path).episode_unsummarized_tail_turns, 9)
            value["context"]["episode_unsummarized_tail_turns"] = 4
            write_app_config(path, value)
            self.assertEqual(load_config(path).episode_unsummarized_tail_turns, 4)

            value["channels"]["primary"] = "missing"  # type: ignore[index]
            write_app_config(path, value)
            with self.assertRaisesRegex(ConfigError, "must name an enabled channel"):
                load_config(path)

    def test_clamped_integer_settings_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            path = root / "config.json"
            value = {
                "providers": "providers.yaml",
                "channels": _napcat_channels(),
                "context": {},
                "storage": {"database": "momoi.sqlite3"},
                "logging": {},
            }
            write_app_config(path, value)
            invalid = {
                ("context", "max_input_tokens"): 999,
                ("context", "transcript_turns_min"): 0,
                ("context", "transcript_turns_max"): 31,
                ("context", "episode_unsummarized_tail_turns"): 0,
                ("context", "memory_results"): 7,
                ("context", "summary_results"): 13,
                ("context", "summary_tokens"): -1,
                ("tools", "result_max_chars"): 999,
                ("turn", "max_total_tokens"): -1,
                ("turn", "max_protocol_retries"): 0,
                ("current_state", "max_seconds"): 0,
            }
            for (section, setting), invalid_value in invalid.items():
                with self.subTest(setting=f"{section}.{setting}"):
                    candidate = json.loads(json.dumps(value))
                    candidate.setdefault(section, {})[setting] = invalid_value
                    write_app_config(path, candidate)
                    with self.assertRaisesRegex(
                        ConfigError, rf"{section}\.{setting} must"
                    ):
                        load_config(path)

            value["context"]["memory_results"] = 1.5  # type: ignore[index]
            write_app_config(path, value)
            with self.assertRaisesRegex(
                ConfigError, "context.memory_results must be an integer"
            ):
                load_config(path)

    def test_loads_primary_channel_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            (root / "prompts" / "HEARTBEAT.md").write_text("偶尔整理自己的摄影兴趣。")
            path = root / "config.json"
            write_app_config(
                path,
                {
                    "providers": "providers.yaml",
                    "channels": _napcat_channels(),
                    "context": {},
                    "tools": {"result_retention_days": 14},
                    "storage": {"database": "momoi.sqlite3"},
                    "logging": {},
                },
            )
            config = load_config(path)
            self.assertIsInstance(config.channel, NapCatConfig)
            self.assertEqual(config.channel.owner_qq, "123")
            self.assertFalse(config.reflection.enabled)
            self.assertEqual(config.reflection.at, "03:00")
            self.assertEqual(config.heartbeat_prompt, "偶尔整理自己的摄影兴趣。")
            self.assertEqual(
                config.heartbeat_prompt_path,
                (root / "prompts" / "HEARTBEAT.md").resolve(),
            )
            self.assertEqual(config.heartbeat.max_interval_seconds, 5400)
            self.assertFalse(hasattr(config, "autonomy"))
            self.assertFalse(
                hasattr(config.heartbeat, "reply_initial_interval_seconds")
            )
            self.assertFalse(
                hasattr(config.heartbeat, "reply_followup_interval_seconds")
            )
            self.assertEqual(config.dashboard.token, "")
            self.assertEqual(config.tool_result_max_chars, 12000)
            self.assertEqual(config.tool_result_retention_days, 14)

            (root / "prompts" / "HEARTBEAT.md").unlink()
            self.assertEqual(load_config(path).heartbeat_prompt, "")

            legacy = json.loads(path.read_text())
            legacy["napcat"] = legacy.pop("channels")["enabled"]["napcat"]
            write_app_config(path, legacy)
            with self.assertRaisesRegex(ConfigError, "unknown configuration field"):
                load_config(path)

    def test_loads_dashboard_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            path = root / "config.json"
            write_app_config(
                path,
                {
                    "providers": "providers.yaml",
                    "channels": _napcat_channels(),
                    "dashboard": {"token": "dash-secret"},
                    "context": {},
                    "storage": {"database": "momoi.sqlite3"},
                    "logging": {},
                },
            )
            config = load_config(path)
            self.assertIsInstance(config.dashboard, DashboardConfig)
            self.assertEqual(config.dashboard.token, "dash-secret")

    def test_environment_overrides_deployment_fields_but_not_llm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            path = root / "config.json"
            write_app_config(
                path,
                {
                    "providers": "providers.yaml",
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
                    "timezone": "UTC",
                    "notifications": {},
                    "context": {},
                    "storage": {"database": "momoi.sqlite3"},
                    "logging": {},
                },
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
                },
                clear=False,
            ):
                config = load_config(path)
            self.assertEqual(config.channel.plugin, "weixin")
            napcat = next(
                item for item in config.channel_configs if item.plugin == "napcat"
            )
            self.assertEqual(napcat.url, "ws://napcat:3001")
            self.assertEqual(napcat.owner_qq, "999")
            self.assertEqual(config.timezone, "Asia/Shanghai")
            self.assertEqual(config.dashboard.token, "env-dash")
            self.assertTrue(config.webhooks.enabled)
            self.assertEqual(config.webhooks.host, "0.0.0.0")
            self.assertEqual(config.webhooks.token, "env-hook")

    def test_dashboard_flag_requires_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            path = root / "config.json"
            write_app_config(
                path,
                {
                    "providers": "providers.yaml",
                    "channels": _napcat_channels(),
                    "context": {},
                    "storage": {"database": "momoi.sqlite3"},
                    "logging": {},
                },
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
                "providers": "providers.yaml",
                "channels": _napcat_channels(),
                "context": {},
                "storage": {"database": "momoi.sqlite3"},
                "logging": {},
            }
            for section in ("webhooks", "heartbeat", "reflection"):
                config[section] = {"enabled": "false"}
                write_app_config(path, config)
                with self.assertRaisesRegex(
                    ConfigError, rf"{section}\.enabled must be boolean"
                ):
                    load_config(path)
                del config[section]

    def test_cli_workspace_defaults_and_can_be_overridden(self) -> None:
        with patch("momoi.__main__.version", return_value="0.1.0"):
            with patch("sys.argv", ["momoi", "run"]):
                args = parse_args()
                self.assertEqual(args.workspace, Path.home() / ".momoi")
                self.assertTrue(args.dashboard)
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
                    "sys.argv", ["momoi", "--workspace", directory]
                ),
            ):
                self.assertEqual(parse_args().workspace, Path(directory))
            with patch("sys.argv", ["momoi"]):
                self.assertTrue(parse_args().dashboard)
            for command in ("goal", "emotion", "embedding", "channel"):
                with self.subTest(command=command), patch("sys.argv", ["momoi", command]):
                    with self.assertRaises(SystemExit) as error:
                        parse_args()
                    self.assertEqual(error.exception.code, 2)

    def test_goal_create_rejects_empty_unused_timestamp(self) -> None:
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
                            "times": ["07:30"],
                        },
                    },
                ),
                TurnDraft(),
                source_event_id="test",
            )
            self.assertFalse(result["ok"], result)
            self.assertIn("next_review_at", result["invalid_fields"])
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
                            "off": {"command": "off-server", "disabled": True, "description": "Off tools"},
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

            value["mcpServers"]["search"]["description"] = "Search"
            value["mcpServers"]["search"]["optional"] = "yes"
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "optional must be boolean"):
                load_mcp_servers(path)
