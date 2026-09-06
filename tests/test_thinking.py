from tests.support import write_app_config
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from momoi.config.loading import load_config
from momoi.observability.context import log_context
from momoi.tools.memory import MemoryTools
from momoi.models import ToolCall, TurnDraft
from momoi.llm.telemetry import (
    anthropic_reasoning,
    openai_reasoning,
    persist_thinking,
)
from momoi.storage import Store
from momoi.storage.thinking import (
    decode_reasoning,
    encode_reasoning,
    month_key,
)
from momoi.tools.thinking import ThinkingTools


def _config(directory: Path, thinking: str | None = None) -> Path:
    path = directory / "config.json"
    storage = {"database": "data/momoi.sqlite3"}
    if thinking is not None:
        storage["thinking"] = thinking
    write_app_config(path, {
                "providers": "providers.yaml",
                "channels": {
                    "primary": "napcat",
                    "enabled": {
                        "napcat": {"url": "ws://localhost", "owner_qq": "1"},
                    },
                },
                "context": {},
                "storage": storage,
                "logging": {},
            })
    (directory / "prompts").mkdir()
    (directory / "prompts" / "SOUL.md").write_text("soul")
    return path


class ThinkingStoreTests(unittest.TestCase):
    def test_config_defaults_thinking_to_database_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config(_config(root))
            self.assertEqual(config.thinking, config.database.parent)

    def test_config_resolves_custom_thinking_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config(_config(root, "thoughts"))
            self.assertEqual(config.thinking, (root / "thoughts").resolve())

    def test_encodes_short_text_plain_and_long_text_gzip(self) -> None:
        codec, blob = encode_reasoning("短")
        self.assertEqual(codec, "plain")
        self.assertEqual(decode_reasoning(codec, blob), "短")
        long_text = "衣服洗好了。" * 200
        codec, blob = encode_reasoning(long_text)
        self.assertEqual(codec, "gzip")
        self.assertLess(len(blob), len(long_text.encode()))
        self.assertEqual(decode_reasoning(codec, blob), long_text)

    def test_records_and_reads_by_turn_and_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            now = time.time()
            store.record_thinking_call(
                created_at=now,
                turn_id="turn-laundry",
                call_id="call-1",
                stage="webhook",
                round=1,
                model="test",
                tools=["end_turn"],
                reasoning="适用性检查后决定静默，不发送衣服洗好了提醒。",
            )
            found = store.search_thinking(query="衣服洗好了")
            self.assertTrue(found["ok"])
            self.assertEqual(found["count"], 1)
            self.assertEqual(found["calls"][0]["turn_id"], "turn-laundry")
            self.assertEqual(found["calls"][0]["tools"], ["end_turn"])
            self.assertIn("衣服洗好了", found["calls"][0]["excerpt"])
            read = store.read_thinking("turn-laundry")
            self.assertTrue(read["ok"])
            self.assertIn("静默", read["calls"][0]["reasoning"])
            month = month_key(now, store.timezone)
            self.assertTrue(
                (Path(directory) / f"thinking-{month}.sqlite3").is_file()
            )
            store.close()

    def test_search_can_use_turn_id_without_scanning_unrelated_months(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store._db.execute(
                """INSERT INTO turns
                   (id, kind, source_ids_json, state, started_at, updated_at)
                   VALUES ('turn-old', 'owner', '[]', 'completed', ?, ?)""",
                (time.time(), time.time()),
            )
            store._db.commit()
            store.record_thinking_call(
                created_at=time.time(),
                turn_id="turn-old",
                call_id="call-2",
                stage="owner",
                round=1,
                reasoning="老师在问为什么没提醒。",
            )
            found = store.search_thinking(turn_id="turn-old")
            self.assertEqual(found["count"], 1)
            store.close()

    def test_tools_search_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.record_thinking_call(
                created_at=time.time(),
                turn_id="webhook:abc:0",
                call_id="call-3",
                stage="webhook",
                round=1,
                tools=["end_turn"],
                reasoning="烘干结束提醒并非老师要求，所以这次也静默。",
            )
            tools = ThinkingTools(store)
            searched = tools.execute(
                ToolCall(
                    "t1",
                    "thinking_search",
                    {"query": "静默", "time_range": {"kind": "recent", "days": 7}},
                )
            )
            self.assertTrue(searched["ok"])
            self.assertEqual(searched["count"], 1)
            read = tools.execute(
                ToolCall("t2", "thinking_read", {"turn_id": "webhook:abc:0"})
            )
            self.assertTrue(read["ok"])
            self.assertIn("静默", read["calls"][0]["reasoning"])
            turn_scoped = tools.execute(
                ToolCall("t3", "thinking_search", {"turn_id": "webhook:abc:0"}),
            )
            self.assertEqual(turn_scoped["time_range"], {"kind": "turn"})
            memory_result = MemoryTools(store).execute(
                ToolCall("t4", "thinking_search", {"turn_id": "webhook:abc:0"}),
                [],
                TurnDraft(),
            )
            self.assertEqual(memory_result["error"], "tool_not_allowed")
            store.close()

    def test_extracts_and_persists_provider_reasoning(self) -> None:
        self.assertEqual(
            openai_reasoning({"reasoning_content": "先核对记忆"}),
            "先核对记忆",
        )
        self.assertEqual(
            anthropic_reasoning(
                [
                    {"type": "thinking", "thinking": "先想一步"},
                    {"type": "text", "text": "hello"},
                    {"type": "redacted_thinking", "data": "hidden"},
                ]
            ),
            "先想一步",
        )
        recorded: dict[str, object] = {}

        def sink(**kwargs: object) -> None:
            recorded.update(kwargs)

        with log_context(turn_id="turn-1", call_id="call-1", stage="owner", round=2):
            persist_thinking(
                sink, reasoning="决定提醒", tools=["send_bubbles"], model="test"
            )
        self.assertEqual(recorded["turn_id"], "turn-1")
        self.assertEqual(recorded["call_id"], "call-1")
        self.assertEqual(recorded["stage"], "owner")
        self.assertEqual(recorded["round"], 2)
        self.assertEqual(recorded["tools"], ["send_bubbles"])
        self.assertEqual(recorded["reasoning"], "决定提醒")

    def test_writes_into_the_month_of_created_at(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            july = datetime(2026, 7, 16, 12, tzinfo=ZoneInfo("UTC"))
            store.record_thinking_call(
                created_at=july.timestamp(),
                turn_id="turn-july",
                call_id="call-july",
                reasoning="七月的思考",
            )
            self.assertTrue((Path(directory) / "thinking-2026-07.sqlite3").is_file())
            store.close()

    def test_timezone_change_reads_adjacent_legacy_month(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            created_at = datetime(
                2026, 1, 31, 20, tzinfo=ZoneInfo("UTC")
            ).timestamp()
            store = Store(path, timezone="UTC")
            store.record_thinking_call(
                created_at=created_at,
                turn_id="turn-month-boundary",
                call_id="call-month-boundary",
                stage="owner",
                reasoning="跨时区月界线仍然可读",
            )
            store.close()

            store = Store(path, timezone="Asia/Shanghai")
            found = store.search_thinking(
                query="跨时区月界线",
                after=datetime(
                    2026, 2, 1, tzinfo=ZoneInfo("Asia/Shanghai")
                ).timestamp(),
                before=datetime(
                    2026, 2, 2, tzinfo=ZoneInfo("Asia/Shanghai")
                ).timestamp(),
            )
            self.assertEqual(found["count"], 1)
            self.assertEqual(found["calls"][0]["turn_id"], "turn-month-boundary")
            store.close()

    def test_dashboard_thinking_defaults_to_the_current_month(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            store.record_thinking_call(
                created_at=time.time(),
                turn_id="turn-dash",
                call_id="call-dash",
                stage="owner",
                reasoning="看板只读查看当时的判断。",
            )
            store.record_thinking_call(
                created_at=time.time() + 1,
                turn_id="turn-dash",
                call_id="call-dash-2",
                stage="owner",
                round=2,
                tools=["end_turn"],
                reasoning="收束。",
            )
            listed = store.dashboard_thinking()
            self.assertTrue(listed["ok"])
            self.assertEqual(listed["count"], 1)
            self.assertEqual(listed["items"][0]["turn_id"], "turn-dash")
            self.assertEqual(listed["items"][0]["call_count"], 2)
            self.assertEqual(listed["items"][0]["stages"], ["owner"])
            self.assertIn(listed["month"], listed["months"])
            store.close()
