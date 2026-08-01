import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


from momoi.__main__ import emotion as emotion_command, goal as goal_command, parse_args
from momoi.agenda_tools import AgendaTools
from momoi.channel.napcat import NapCatConfig
from momoi.channel.weixin import WeixinConfig
from momoi.config import (
    ConfigError,
    NotificationConfig,
    load_config,
)
from momoi.mcp_client import load_mcp_servers
from momoi.models import (
    ToolCall,
    TurnDraft,
)
from momoi.storage import Store


class ConfigurationTest(unittest.TestCase):
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
            self.assertEqual(
                [type(item) for item in config.channel_configs],
                [NapCatConfig, WeixinConfig],
            )

            value["channels"]["primary"] = "missing"  # type: ignore[index]
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ConfigError, "must name an enabled channel"):
                load_config(path)

            value["channels"]["primary"] = "napcat"  # type: ignore[index]
            value["channel"] = {"plugin": "weixin", "settings": {}}
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ConfigError, "either channel or channels"):
                load_config(path)

    def test_loads_channel_plugin_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            (root / "HEARTBEAT.md").write_text("偶尔整理自己的摄影兴趣。")
            path = root / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "llm": {
                            "base_url": "https://example.com",
                            "api_key": "key",
                            "model": "model",
                        },
                        "channel": {
                            "plugin": "napcat",
                            "settings": {
                                "url": "ws://localhost",
                                "owner_qq": "123",
                            },
                        },
                        "context": {},
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
            self.assertEqual(config.heartbeat.max_interval_seconds, 5400)
            self.assertEqual(config.heartbeat.reply_initial_interval_seconds, 60)

            (root / "HEARTBEAT.md").unlink()
            self.assertEqual(load_config(path).heartbeat_prompt, "")

            legacy = json.loads(path.read_text())
            legacy["napcat"] = legacy.pop("channel")["settings"]
            path.write_text(json.dumps(legacy))
            with self.assertRaisesRegex(ConfigError, "channel must be a table/object"):
                load_config(path)

    def test_config_rejects_string_booleans(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts" / "SOUL.md").write_text("Test soul")
            path = root / "config.json"
            config = {
                "llm": {"base_url": "https://example.com", "api_key": "key", "model": "model"},
                "channel": {
                    "plugin": "napcat",
                    "settings": {"url": "ws://localhost", "owner_qq": "123"},
                },
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
            fake_config = SimpleNamespace(database=database)

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
        with patch("sys.argv", ["momoi", "run"]):
            self.assertEqual(parse_args().workspace, Path.home() / ".momoi")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("sys.argv", ["momoi", "--workspace", directory, "emotion", "list"]),
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
                            "at": "07:30",
                            "every_seconds": 86400,
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
                            "search": {"command": "search-server"},
                            "off": {"command": "off-server", "disabled": True},
                        }
                    }
                )
            )
            self.assertEqual(list(load_mcp_servers(path)), ["search"])
