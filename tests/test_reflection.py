import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from momoi.channel.napcat import NapCatConfig
from momoi.config import AppConfig, LLMConfig, NotificationConfig, ReflectionConfig
from momoi.runtime import REFLECTION_FINISH_SPEC, MomoiDaemon
from momoi.models import ProviderResponse, ToolCall


class ReflectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_daily_reflection_promotes_only_evidence_backed_learning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 1000, 0, 1, 0),
                channel=NapCatConfig(
                    "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                ),
                system_prompt="test {{SOUL}} {{CAPABILITY_POLICIES}}",
                soul_prompt="Test soul",
                recent_raw_tokens=8000,
                recent_turns=2,
                memory_results=4,
                memory_tokens=4000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="DEBUG",
                notifications=NotificationConfig(timezone="Asia/Shanghai"),
                reflection=ReflectionConfig(enabled=True),
            )
            daemon = MomoiDaemon(config)
            now = datetime(2026, 7, 22, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            claimed = daemon.store.claim_due_reflection(
                config.reflection, config.notifications.timezone, now.timestamp()
            )
            self.assertEqual(claimed["local_date"], "2026-07-21")
            occurred = datetime(
                2026, 7, 21, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")
            ).timestamp()
            daemon.store._db.execute(
                """INSERT INTO messages
                   (role, content, created_at, source_event_ids_json)
                   VALUES ('user', ?, ?, '[]')""",
                (
                    (
                        "# Current owner messages\n我不吃香菜，今天看了项目资料。"
                        "回答直接说结论就好。"
                    ),
                    occurred,
                ),
            )
            daemon.store._db.execute(
                """INSERT INTO turns
                   (id, kind, source_ids_json, state, started_at, updated_at)
                   VALUES ('tool-turn', 'owner', '[]', 'completed', ?, ?)""",
                (occurred, occurred),
            )
            daemon.store._db.execute(
                """INSERT INTO tool_audit
                   (turn_id, tool_call_id, tool_name, arguments_sha256, state,
                    result_json, ok, started_at, completed_at, capability)
                   VALUES ('tool-turn', 'mail-search', 'mcp__gog__gmail_search',
                           'hash', 'completed', ?, 1, ?, ?, 'read')""",
                ('{"subject":"project summary"}', occurred, occurred),
            )
            daemon.store._db.commit()

            class Provider:
                async def complete(
                    self,
                    _system: object,
                    _messages: object,
                    tools: list[dict[str, object]],
                    **_kwargs: object,
                ) -> ProviderResponse:
                    assert tools == [REFLECTION_FINISH_SPEC]
                    request = json.dumps(_messages, ensure_ascii=False)
                    assert "<daily_reflection_record>" in request
                    assert "<runtime_state>" in request
                    assert (
                        "state=completed ok=true capability=read" in request
                    )
                    assert "interaction.*" in json.dumps(_system, ensure_ascii=False)
                    call = ToolCall(
                        "finish-reflection",
                        "reflection_finish",
                        {
                            "summary": "主人明确表达了饮食偏好，今后推荐食物时应留意。",
                            "memories": [
                                {
                                    "kind": "owner_preference",
                                    "key": "food.avoids_cilantro",
                                    "content": "主人不吃香菜。",
                                    "evidence": "我不吃香菜",
                                    "confidence": 1,
                                },
                                {
                                    "kind": "practice",
                                    "key": "interaction.answer_brevity",
                                    "content": (
                                        "When the owner asks for a direct answer, lead with the "
                                        "conclusion and avoid unsolicited expansion."
                                    ),
                                    "evidence": "回答直接说结论就好",
                                    "confidence": 0.6,
                                }
                            ],
                        },
                    )
                    return ProviderResponse(
                        [
                            {
                                "type": "tool_use",
                                "id": call.id,
                                "name": call.name,
                                "input": call.arguments,
                            }
                        ],
                        [call],
                    )

            daemon.provider = Provider()
            await daemon._complete_reflection_turn("2026-07-21", asyncio.Event())
            reflection = daemon.store.reflection("2026-07-21")
            self.assertEqual(reflection["state"], "completed")
            self.assertEqual(
                json.loads(reflection["memories_json"])[0]["key"],
                "food.avoids_cilantro",
            )
            self.assertIn(
                "主人不吃香菜",
                daemon.store.reflection_memory_context("香菜", 4, 2000),
            )
            self.assertIn(
                "lead with the conclusion",
                daemon.store.reflection_memory_context("直接回答", 4, 2000),
            )
            self.assertEqual(
                daemon.store.next_reflection_due_at(
                    config.reflection,
                    config.notifications.timezone,
                    now.timestamp(),
                ),
                datetime(
                    2026, 7, 23, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai")
                ).timestamp(),
            )
            daemon.store.close()

    async def test_owner_learning_requires_owner_evidence(self) -> None:
        result, error = MomoiDaemon._parse_reflection_finish(
            {
                "summary": "测试",
                "memories": [
                    {
                        "kind": "owner_profile",
                        "key": "owner.job",
                        "content": "主人是医生。",
                        "evidence": "主人是医生",
                        "confidence": 0.8,
                    }
                ],
            },
            "[MOMOI]\n主人是医生",
            "",
            "",
        )
        self.assertIsNone(result)
        self.assertEqual(error, "owner_reflection_requires_owner_evidence")

    def test_interaction_practice_requires_owner_feedback_and_one_item(self) -> None:
        memory = {
            "kind": "practice",
            "key": "interaction.answer_brevity",
            "content": "When asked directly, answer directly.",
            "evidence": "直接说结论就好",
            "confidence": 0.7,
        }
        result, error = MomoiDaemon._parse_reflection_finish(
            {"summary": "测试", "memories": [memory]},
            "[MOMOI]\n直接说结论就好",
            "",
            "",
        )
        self.assertIsNone(result)
        self.assertEqual(error, "interaction_practice_requires_owner_evidence")

        second = {
            **memory,
            "key": "interaction.no_unsolicited_lists",
        }
        result, error = MomoiDaemon._parse_reflection_finish(
            {"summary": "测试", "memories": [memory, second]},
            "[OWNER]\n直接说结论就好",
            "直接说结论就好",
            "",
        )
        self.assertIsNone(result)
        self.assertEqual(error, "too_many_interaction_practices")

        result, error = MomoiDaemon._parse_reflection_finish(
            {"summary": "测试", "memories": [memory]},
            "[OWNER]\n直接说结论就好",
            "直接说结论就好",
            "",
        )
        self.assertIsNone(error)
        self.assertEqual(result["memories"][0]["key"], memory["key"])
