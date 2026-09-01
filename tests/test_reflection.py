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
from momoi.runtime.parsing import parse_reflection_finish
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
                        "我不吃香菜，今天看了项目资料。回答直接说结论就好。"
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
            daemon.store.append_turn_journal(
                "tool-turn",
                "tool_call",
                {
                    "tool_call_id": "mail-search",
                    "name": "mcp__gog__gmail_search",
                    "source": "mcp",
                    "arguments": {"query": "project summary", "limit": 10},
                },
                created_at=occurred + 1,
            )
            daemon.store.append_turn_journal(
                "tool-turn",
                "tool_result",
                {
                    "tool_call_id": "mail-search",
                    "name": "mcp__gog__gmail_search",
                    "ok": True,
                    "error": None,
                    "result": {"messages": [{"subject": "project summary"}]},
                },
                trust="untrusted_tool_data",
                created_at=occurred + 2,
            )

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
                    assert "Daily reflection contract" not in json.dumps(
                        _system, ensure_ascii=False
                    )
                    assert "<workflow_contract>" in request
                    assert "Daily reflection contract" in request
                    assert "<daily_reflection_record>" in request
                    assert "<tool_timeline>" in request
                    assert "arguments=" in request
                    assert "project summary" in request
                    assert "result_trust=untrusted_tool_data" in request
                    assert "<runtime_state>" in request
                    assert "<open_conversations>" in request
                    assert "<always_memory_inventory>" not in request
                    assert "<recent_memory_inventory>" not in request
                    assert "No open or closing conversations are stored." in request
                    assert (
                        "state=completed ok=true capability=read" in request
                    )
                    schema = json.dumps(tools, ensure_ascii=False)
                    assert "grounded, thoughtful Chinese diary" in schema
                    assert "Use tool_skill" in schema
                    assert "Use practice" in schema
                    assert "open_conversations" in schema
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
                                },
                                {
                                    "kind": "tool_skill",
                                    "key": "tools.gmail.search_project_summary",
                                    "content": (
                                        "Search project-summary mail with "
                                        "mcp__gog__gmail_search and verify returned subjects."
                                    ),
                                    "evidence": "project summary",
                                    "confidence": 0.8,
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
            maintenance = await daemon.autonomous.get()
            self.assertEqual(maintenance.kind, "memory_maintenance")
            self.assertEqual(
                daemon.store.pending_memory_maintenance_turn(),
                maintenance.id,
            )
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
            self.assertIn(
                "mcp__gog__gmail_search",
                daemon.store.reflection_memory_context("gmail", 4, 2000),
            )
            rendered_reflection = daemon.store.reflection_memory_context(
                "gmail", 4, 2000
            )
            self.assertIn(
                "may be outdated or no longer applicable", rendered_reflection
            )
            self.assertIn("[date=2026-07-21 tool_skill:", rendered_reflection)
            _, ranked_reflection = daemon.store.ranked_memory_context(
                "gmail", 4, 2000
            )
            self.assertIn("mcp__gog__gmail_search", ranked_reflection)
            self.assertIn("[date=2026-07-21 tool_skill:", ranked_reflection)
            stored_memories = json.loads(reflection["memories_json"])
            self.assertEqual(stored_memories[2]["kind"], "tool_skill")
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
        result, error = parse_reflection_finish(
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
        result, error = parse_reflection_finish(
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
        result, error = parse_reflection_finish(
            {"summary": "测试", "memories": [memory, second]},
            "[OWNER]\n直接说结论就好",
            "直接说结论就好",
            "",
        )
        self.assertIsNone(result)
        self.assertEqual(error, "too_many_interaction_practices")

        result, error = parse_reflection_finish(
            {"summary": "测试", "memories": [memory]},
            "[OWNER]\n直接说结论就好",
            "直接说结论就好",
            "",
        )
        self.assertIsNone(error)
        self.assertEqual(result["memories"][0]["key"], memory["key"])

    def test_methodology_practice_does_not_require_tool_or_owner_evidence(self) -> None:
        result, error = parse_reflection_finish(
            {
                "summary": "测试",
                "memories": [
                    {
                        "kind": "practice",
                        "key": "debugging.reproduce_before_fix",
                        "content": (
                            "Reproduce the failure before changing code, then rerun "
                            "the same check to verify the fix."
                        ),
                        "evidence": "先复现失败，再修改并用同一个检查验证",
                        "confidence": 0.8,
                    }
                ],
            },
            "[MOMOI]\n先复现失败，再修改并用同一个检查验证",
            "",
            "",
        )
        self.assertIsNone(error)
        self.assertEqual(result["memories"][0]["kind"], "practice")

    def test_confirmed_memory_actions_are_rejected(self) -> None:
        for field in ("always_memory_actions", "recent_memory_actions"):
            result, error = parse_reflection_finish(
                {"summary": "测试", "memories": [], field: []},
                "",
                "",
                "",
            )
            self.assertIsNone(result)
            self.assertEqual(error, "invalid_reflection_finish")

    def test_conversation_actions_are_validated(self) -> None:
        base = {"summary": "测试", "memories": []}
        result, error = parse_reflection_finish(
            {
                **base,
                "conversation_actions": [
                    {
                        "episode_id": "trip-kyoto",
                        "action": "close",
                        "reason": "老师说这趟旅行已经结束。",
                    }
                ],
            },
            "",
            "",
            "",
            {"trip-kyoto", "chat-today"},
        )
        self.assertIsNone(error)
        self.assertEqual(result["conversation_actions"][0]["action"], "close")

        _, error = parse_reflection_finish(
            {
                **base,
                "conversation_actions": [
                    {
                        "episode_id": "missing",
                        "action": "close",
                        "reason": "不在清单里。",
                    }
                ],
            },
            "",
            "",
            "",
            {"trip-kyoto"},
        )
        self.assertEqual(error, "unknown_open_conversation")

        _, error = parse_reflection_finish(
            {
                **base,
                "conversation_actions": [
                    {
                        "episode_id": "trip-kyoto",
                        "action": "keep",
                        "reason": "还没结束。",
                    }
                ],
            },
            "",
            "",
            "",
            {"trip-kyoto"},
        )
        self.assertEqual(error, "invalid_conversation_action")

    async def test_daily_reflection_leaves_confirmed_memories_unchanged(self) -> None:
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
                ("# Current owner messages\n今天已经回家了，上次那趟旅行早就结束了。", occurred),
            )
            seeds = [
                (
                    "preference",
                    "food.avoids_cilantro",
                    "老师希望后续日常回复末尾不使用中文句号。",
                    "不要用句号",
                ),
                (
                    "preference",
                    "food.no_cilantro",
                    "老师希望在日常聊天里不要使用句号，因为句号会显得冷淡。",
                    "句号显得冷淡",
                ),
                (
                    "preference",
                    "current.at_office",
                    "老师今天在公司加班。",
                    "我今天在公司加班",
                ),
                (
                    "preference",
                    "hobby.reading",
                    "老师喜欢读推理小说。",
                    "我喜欢读推理小说",
                ),
                (
                    "episodic",
                    "trip.last_summer",
                    "老师去年夏天在京都旅行。",
                    "我在京都",
                ),
            ]
            ids: dict[str, int] = {}
            for kind, key, content, evidence in seeds:
                cursor = daemon.store._db.execute(
                    """INSERT INTO memories
                       (kind, key, content, activation, authority, source_event_id,
                        evidence_quote, importance, created_at, updated_at)
                       VALUES (?, ?, ?, 'always', 'owner', ?, ?, 0.5, ?, ?)""",
                    (kind, key, content, f"evt-{key}", evidence, occurred, occurred),
                )
                ids[key] = int(cursor.lastrowid)
            recent_expires_at = 2_000_000_000.0
            daemon.store._db.execute(
                """UPDATE memories SET activation='recent', expires_at=?
                   WHERE id=?""",
                (recent_expires_at, ids["current.at_office"]),
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
                    assert "<open_conversations>" in request
                    assert "<always_memory_inventory>" not in request
                    assert "<recent_memory_inventory>" not in request
                    call = ToolCall(
                        "finish-reflection",
                        "reflection_finish",
                        {
                            "summary": "今天回顾了已经结束的旅行和当前状态。",
                            "memories": [],
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
            always = {
                (row["kind"], row["key"]): row
                for row in daemon.store.always_memory_inventory()
            }
            self.assertEqual(len(always), len(seeds) - 1)
            self.assertEqual(
                always[("preference", "food.avoids_cilantro")]["content"],
                "老师希望后续日常回复末尾不使用中文句号。",
            )
            self.assertTrue(
                daemon.store.has_memory("preference", "food.no_cilantro")
            )
            self.assertEqual(
                daemon.store._db.execute(
                    "SELECT activation FROM memories WHERE id=?",
                    (ids["current.at_office"],),
                ).fetchone()["activation"],
                "recent",
            )
            recent_row = daemon.store._db.execute(
                """SELECT expires_at,updated_at FROM memories WHERE id=?""",
                (ids["current.at_office"],),
            ).fetchone()
            self.assertEqual(recent_row["expires_at"], recent_expires_at)
            self.assertEqual(recent_row["updated_at"], occurred)
            self.assertIsNone(
                daemon.store._db.execute(
                    """SELECT 1 FROM memory_tombstones
                       WHERE kind='preference' AND key='current.at_office'"""
                ).fetchone()
            )
            self.assertEqual(
                daemon.store._db.execute(
                    "SELECT activation FROM memories WHERE id=?",
                    (ids["hobby.reading"],),
                ).fetchone()["activation"],
                "always",
            )
            self.assertTrue(daemon.store.has_memory("episodic", "trip.last_summer"))
            daemon.store.close()

    async def test_manual_reflect_overwrites_completed_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 1000, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="test",
                recent_raw_tokens=8000,
                recent_turns=2,
                memory_results=4,
                memory_tokens=4000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="DEBUG",
                notifications=NotificationConfig(timezone="Asia/Shanghai"),
            )
            daemon = MomoiDaemon(config)
            first_now = datetime(
                2026, 7, 21, 12, 54, tzinfo=ZoneInfo("Asia/Shanghai")
            ).timestamp()
            second_now = datetime(
                2026, 7, 21, 13, 7, tzinfo=ZoneInfo("Asia/Shanghai")
            ).timestamp()
            occurred = datetime(
                2026, 7, 21, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")
            ).timestamp()
            daemon.store._db.execute(
                """INSERT INTO messages
                   (role, content, created_at, source_event_ids_json)
                   VALUES ('user', ?, ?, '[]')""",
                ("第一轮记录 第二轮记录", occurred),
            )
            daemon.store._db.execute(
                """INSERT INTO turns
                   (id, kind, source_ids_json, state, started_at, updated_at)
                   VALUES (?, 'autonomous', ?, 'completed', ?, ?)""",
                (
                    daemon._turn_id("reflection", "2026-07-21"),
                    '["reflection:2026-07-21"]',
                    first_now,
                    first_now,
                ),
            )
            daemon.store._db.commit()

            summaries = iter(("第一版日记", "覆盖后的日记"))
            memories = iter(
                (
                    [
                        {
                            "kind": "self_insight",
                            "key": "topic.first_pass",
                            "content": "第一轮晋升。",
                            "evidence": "第一轮记录",
                            "confidence": 0.7,
                        }
                    ],
                    [
                        {
                            "kind": "self_insight",
                            "key": "topic.second_pass",
                            "content": "第二轮覆盖晋升。",
                            "evidence": "第二轮记录",
                            "confidence": 0.8,
                        }
                    ],
                )
            )

            class Provider:
                async def complete(
                    self,
                    _system: object,
                    _messages: object,
                    tools: list[dict[str, object]],
                    **_kwargs: object,
                ) -> ProviderResponse:
                    assert tools == [REFLECTION_FINISH_SPEC]
                    call = ToolCall(
                        "finish-reflection",
                        "reflection_finish",
                        {
                            "summary": next(summaries),
                            "memories": next(memories),
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
            first = daemon.store.claim_manual_reflection(
                config.notifications.timezone, first_now
            )
            self.assertEqual(first["local_date"], "2026-07-21")
            await daemon._complete_reflection_turn("2026-07-21", asyncio.Event())
            first_reflection = daemon.store.reflection("2026-07-21")
            self.assertEqual(first_reflection["state"], "completed")
            self.assertEqual(first_reflection["summary"], "第一版日记")
            self.assertEqual(
                json.loads(first_reflection["memories_json"])[0]["key"],
                "topic.first_pass",
            )
            self.assertIn(
                "第一轮晋升",
                daemon.store.reflection_memory_context("第一轮", 4, 2000),
            )

            second = daemon.store.claim_manual_reflection(
                config.notifications.timezone, second_now
            )
            self.assertEqual(second["state"], "running")
            self.assertNotEqual(first["claimed_at"], second["claimed_at"])
            await daemon._complete_reflection_turn("2026-07-21", asyncio.Event())
            overwritten = daemon.store.reflection("2026-07-21")
            self.assertEqual(overwritten["state"], "completed")
            self.assertEqual(overwritten["summary"], "覆盖后的日记")
            self.assertEqual(
                json.loads(overwritten["memories_json"])[0]["key"],
                "topic.second_pass",
            )
            self.assertIn(
                "第二轮覆盖晋升",
                daemon.store.reflection_memory_context("第二轮", 4, 2000),
            )
            self.assertNotIn(
                "第一轮晋升",
                daemon.store.reflection_memory_context("第一轮", 4, 2000),
            )
            turn_states = {
                row["id"]: row["state"]
                for row in daemon.store._db.execute(
                    """SELECT id, state FROM turns
                       WHERE source_ids_json LIKE '%reflection:2026-07-21%'"""
                )
            }
            self.assertEqual(
                turn_states[daemon._turn_id("reflection", "2026-07-21")],
                "completed",
            )
            self.assertEqual(
                turn_states[
                    daemon._turn_id("reflection", "2026-07-21", first["claimed_at"])
                ],
                "completed",
            )
            self.assertEqual(
                turn_states[
                    daemon._turn_id("reflection", "2026-07-21", second["claimed_at"])
                ],
                "completed",
            )
            daemon.store.close()

    async def test_daily_reflection_closes_finished_open_conversations(self) -> None:
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
                ("# Current owner messages\n上次那趟京都旅行早就结束了。", occurred),
            )
            daemon.store.create_episode(
                "京都旅行",
                episode_id="trip-kyoto",
                open_loops=["等老师分享旅行照片"],
            )
            daemon.store.create_episode("今日闲聊", episode_id="chat-today")
            daemon.store._db.execute(
                """UPDATE conversation_episodes
                   SET working_summary=?, status='closing'
                   WHERE id='trip-kyoto'""",
                ("去年夏天京都旅行，还在等照片。",),
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
                    assert "<open_conversations>" in request
                    assert "episode_id=trip-kyoto" in request
                    assert "episode_id=chat-today" in request
                    assert "等老师分享旅行照片" in request
                    call = ToolCall(
                        "finish-reflection",
                        "reflection_finish",
                        {
                            "summary": "京都旅行已经结束，关掉那条还开着的对话；今天的闲聊还在继续。",
                            "memories": [],
                            "conversation_actions": [
                                {
                                    "episode_id": "trip-kyoto",
                                    "action": "close",
                                    "reason": "老师说这趟旅行已经结束。",
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
            daemon.episode_annealing_requested.clear()
            await daemon._complete_reflection_turn("2026-07-21", asyncio.Event())
            closed = daemon.store.episode("trip-kyoto")
            kept = daemon.store.episode("chat-today")
            self.assertEqual(closed["status"], "closed")
            self.assertIsNotNone(closed["closed_at"])
            self.assertEqual(closed["open_loops"], [])
            self.assertEqual(kept["status"], "open")
            self.assertTrue(daemon.episode_annealing_requested.is_set())
            daemon.store.close()
