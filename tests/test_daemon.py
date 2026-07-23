import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from momoi.channel.napcat import NapCatConfig
from momoi.config import (
    AppConfig,
    AutonomyConfig,
    HeartbeatConfig,
    LLMConfig,
    NotificationConfig,
)
from momoi.runtime import (
    AUTONOMOUS_FINISH_SPEC,
    HEARTBEAT_FINISH_SPEC,
    HEARTBEAT_QUEUE_ITEM,
    RESPOND_TOOL_SPEC,
    SEND_MESSAGE_TOOL_SPEC,
    WEBHOOK_SEND_MESSAGE_TOOL_SPEC,
    MomoiDaemon,
)
from momoi.models import (
    AgentReply,
    IncomingMessage,
    ProviderResponse,
    ToolCall,
    TurnDraft,
)
from momoi.provider import (
    ProviderError,
)
from momoi.storage import estimate_tokens


class DaemonTest(unittest.TestCase):
    def test_mood_transition_parser_rejects_invalid_state(self) -> None:
        mood, error = MomoiDaemon._parse_mood_transition(
            {
                "state": "angry",
                "intensity": 0.8,
                "cause": "test",
                "duration_minutes": 30,
            }
        )
        self.assertIsNone(mood)
        self.assertEqual(error, "invalid_mood_transition")

    def test_mood_decision_is_explicit_in_terminal_tools(self) -> None:
        mood, error = MomoiDaemon._parse_mood_decision({"action": "keep"})
        self.assertIsNone(mood)
        self.assertIsNone(error)
        mood, error = MomoiDaemon._parse_mood_decision(
            {
                "action": "transition",
                "state": "excited",
                "intensity": 0.8,
                "cause": "完成新能力接入",
                "duration_minutes": 30,
            }
        )
        self.assertEqual(mood["state"], "excited")
        self.assertIsNone(error)
        self.assertEqual(
            MomoiDaemon._parse_mood_decision(None),
            (None, "invalid_mood_decision"),
        )
        self.assertIn("mood", RESPOND_TOOL_SPEC["input_schema"]["required"])
        self.assertIn("delivery", RESPOND_TOOL_SPEC["input_schema"]["required"])
        self.assertIn("delivery", SEND_MESSAGE_TOOL_SPEC["input_schema"]["required"])
        self.assertNotIn(
            "delivery", WEBHOOK_SEND_MESSAGE_TOOL_SPEC["input_schema"]["required"]
        )
        self.assertIn("natural turning point", SEND_MESSAGE_TOOL_SPEC["description"])
        self.assertIn("terminal output", WEBHOOK_SEND_MESSAGE_TOOL_SPEC["description"])
        self.assertIn("mood", HEARTBEAT_FINISH_SPEC["input_schema"]["required"])

    def test_context_budget_drops_old_history_and_truncates_tool_results(self) -> None:
        daemon = object.__new__(MomoiDaemon)
        daemon.config = SimpleNamespace(max_input_tokens=5000)
        messages = [
            {"role": "user", "content": "旧历史" * 4000},
            {"role": "user", "content": "当前消息"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "large-result",
                        "name": "read_file",
                        "input": {},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "large-result",
                        "content": "结果" * 12000,
                    }
                ],
            },
        ]
        remaining = daemon._fit_context(
            [{"type": "text", "text": "system"}], messages, [], 1
        )
        estimated = estimate_tokens(json.dumps(messages, ensure_ascii=False))
        self.assertEqual(remaining, 0)
        self.assertEqual(messages[0]["content"], "当前消息")
        self.assertLessEqual(estimated, 5000)


class DaemonAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_owner_updates_interrupt_after_tool_and_before_respond(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig(
                        "http://127.0.0.1", "test", "test", 100, 0, 1, 0
                    ),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    recent_raw_tokens=1000,
                    recent_turns=2,
                    memory_results=2,
                    memory_tokens=1000,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            tool_started = asyncio.Event()
            finish_tool = asyncio.Event()
            stale_respond_started = asyncio.Event()
            finish_stale_respond = asyncio.Event()

            async def execute_tool(call: ToolCall) -> dict[str, object]:
                self.assertEqual(call.name, "read_file")
                tool_started.set()
                await finish_tool.wait()
                return {"ok": True, "content": "旧地址天气"}

            daemon.builtin_tools.execute = execute_tool  # type: ignore[method-assign]

            class Provider:
                calls = 0

                async def complete(
                    provider_self,
                    _: object,
                    messages: list[dict[str, object]],
                    __: object,
                    **___: object,
                ) -> ProviderResponse:
                    provider_self.calls += 1
                    if provider_self.calls == 1:
                        call = ToolCall(
                            "weather-read", "read_file", {"path": "weather.txt"}
                        )
                    elif provider_self.calls == 2:
                        self.assertIn("地址改成上海", json.dumps(messages, ensure_ascii=False))
                        stale_respond_started.set()
                        await finish_stale_respond.wait()
                        call = ToolCall(
                            "stale-respond",
                            "respond",
                            {
                                "delivery": "报告上海天气",
                                "messages": ["上海天气晴"],
                                "continuity": {
                                    "topic": "天气",
                                    "open_loops": [],
                                    "pending_commitments": [],
                                    "short_term_facts": [],
                                },
                                "mood": {"action": "keep"},
                            },
                        )
                    else:
                        rendered = json.dumps(messages, ensure_ascii=False)
                        self.assertIn("不用查天气了", rendered)
                        self.assertIn("superseded_by_owner_update", rendered)
                        call = ToolCall(
                            "final-respond",
                            "respond",
                            {
                                "delivery": "确认采用最新要求",
                                "messages": ["收到，不查了"],
                                "continuity": {
                                    "topic": "",
                                    "open_loops": [],
                                    "pending_commitments": [],
                                    "short_term_facts": [],
                                },
                                "mood": {"action": "keep"},
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

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            initial = IncomingMessage("qq:turn:1", "1", "查旧地址天气", 1, 1)
            first_update = IncomingMessage("qq:turn:2", "2", "地址改成上海", 2, 2)
            second_update = IncomingMessage(
                "qq:turn:3", "3", "不用查天气了，只告诉我你收到了", 3, 3
            )
            daemon.store.add_event(initial)
            turn_id = daemon._turn_id(initial.event_id)
            turn = asyncio.create_task(
                daemon._complete_batch_turn([initial], asyncio.Event(), turn_id)
            )

            await tool_started.wait()
            await daemon._receive(first_update)
            finish_tool.set()
            await stale_respond_started.wait()
            await daemon._receive(second_update)
            finish_stale_respond.set()
            await turn

            self.assertEqual(provider.calls, 3)
            self.assertTrue(daemon.incoming.empty())
            self.assertEqual([row.text for row in daemon.store.due_outbox()], ["收到，不查了"])
            stored = daemon.store._db.execute(
                "SELECT content, source_event_ids_json FROM messages WHERE role='user'"
            ).fetchone()
            self.assertIn("地址改成上海", stored["content"])
            self.assertIn("不用查天气了", stored["content"])
            self.assertEqual(
                json.loads(stored["source_event_ids_json"]),
                [initial.event_id, first_update.event_id, second_update.event_id],
            )
            stored_turn = daemon.store._db.execute(
                "SELECT source_ids_json FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            self.assertEqual(
                json.loads(stored_turn["source_ids_json"]),
                [initial.event_id, first_update.event_id, second_update.event_id],
            )
            self.assertEqual(daemon.store.pending_events(), [])
            daemon.store.close()

    async def test_manual_heartbeat_command_queues_once_even_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig(
                        "http://127.0.0.1", "test", "test", 100, 0, 1, 0
                    ),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    recent_raw_tokens=1000,
                    recent_turns=2,
                    memory_results=2,
                    memory_tokens=1000,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            command = IncomingMessage(
                "qq:manual-heartbeat", "manual-heartbeat", "/heartbeat", 1, 1
            )
            await daemon._receive(command)
            await daemon._receive(command)

            self.assertEqual(await daemon.autonomous.get(), HEARTBEAT_QUEUE_ITEM)
            self.assertTrue(daemon.autonomous.empty())
            self.assertEqual(daemon.store.pending_events(), [])
            self.assertIsNotNone(daemon.store.self_state()["heartbeat_claimed_at"])
            daemon.store.close()

    async def test_goal_commits_only_after_explicit_autonomous_finish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig(
                        "http://127.0.0.1", "test", "test", 100, 0, 1, 0, "openai"
                    ),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    recent_raw_tokens=1000,
                    recent_turns=2,
                    memory_results=2,
                    memory_tokens=1000,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                    autonomy=AutonomyConfig(
                        ("curl", "read_file", "write_file", "mcp__test__read")
                    ),
                )
            )
            event = IncomingMessage("qq:goal-finish", "goal-finish", "继续检查", 1, 1)
            daemon.store.add_event(event)
            draft = TurnDraft()
            created = daemon.agenda_tools.execute(
                ToolCall(
                    "create",
                    "goal_create",
                    {
                        "title": "检查任务",
                        "success_criteria": "记录检查结果",
                        "next_action": "执行检查",
                        "next_review_at": (
                            datetime.now().astimezone() + timedelta(milliseconds=20)
                        ).isoformat(),
                    },
                ),
                draft,
                authority="agent",
                source_event_id=event.event_id,
                allow_notify=False,
            )
            goal_id = str(created["goal"]["id"])
            daemon.store.commit_turn([event], event.text, AgentReply(["好"]), draft)
            daemon.mcp.tool_specs = [
                {
                    "name": "mcp__test__read",
                    "description": "read",
                    "input_schema": {"type": "object"},
                },
                {
                    "name": "mcp__test__write",
                    "description": "write",
                    "input_schema": {"type": "object"},
                },
            ]
            daemon.mcp._capabilities = {
                "mcp__test__read": "read",
                "mcp__test__write": "external_effect",
            }
            await asyncio.sleep(0.03)
            daemon.store.claim_due_goal()

            class Provider:
                calls = 0

                async def complete(
                    self,
                    _: object,
                    messages: object,
                    tools: list[dict[str, object]],
                    **kwargs: object,
                ) -> ProviderResponse:
                    self.calls += 1
                    if not kwargs.get("require_tool"):
                        raise AssertionError("autonomous turns must require a terminal tool")
                    if self.calls == 1:
                        names = {str(tool["name"]) for tool in tools}
                        if "mcp__test__read" not in names or {
                            "mcp__test__write",
                            "reminder_create",
                        } & names:
                            raise AssertionError(names)
                        if "write_file" not in names:
                            raise AssertionError(names)
                        call = ToolCall(
                            "outside-artifact",
                            "write_file",
                            {"path": "/tmp/not-allowed", "content": "no"},
                        )
                    elif self.calls == 2:
                        if "path_outside_autonomous_artifacts" not in json.dumps(
                            messages
                        ):
                            raise AssertionError(messages)
                        call = ToolCall(
                            "update",
                            "goal_update",
                            {
                                "goal_id": goal_id,
                                "status": "waiting",
                                "waiting_for": "下一次检查",
                                "latest_result": "本次检查正常",
                                "next_review_at": (
                                    datetime.now().astimezone() + timedelta(hours=1)
                                ).isoformat(),
                            },
                        )
                    elif self.calls == 3:
                        call = ToolCall(
                            "notify",
                            "owner_notify",
                            {
                                "text": "检查完成，目前正常",
                                "reason": "任务阶段结果",
                                "key": "service.check",
                            },
                        )
                    else:
                        if [tool["name"] for tool in tools] == ["respond"]:
                            raise AssertionError("goal must not use owner respond")
                        call = ToolCall("finish", "autonomous_finish", {})
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

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            await daemon._complete_goal_turn(goal_id, asyncio.Event())

            self.assertEqual(provider.calls, 4)
            self.assertEqual(daemon.store.goal(goal_id)["latest_result"], "本次检查正常")
            notification = daemon.store._db.execute(
                "SELECT state, messages_json FROM notifications WHERE goal_id=?",
                (goal_id,),
            ).fetchone()
            self.assertEqual(notification["state"], "pending")
            self.assertIn("检查完成", notification["messages_json"])
            turn = daemon.store._db.execute(
                "SELECT state FROM turns WHERE source_ids_json=?",
                (json.dumps([f"goal:{goal_id}"]),),
            ).fetchone()
            self.assertEqual(turn["state"], "completed")
            self.assertEqual(AUTONOMOUS_FINISH_SPEC["name"], "autonomous_finish")
            daemon.store.close()

    async def test_repeated_invalid_tools_force_a_visible_failure_response(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                    system_prompt="test",
                    recent_raw_tokens=1000,
                    recent_turns=2,
                    memory_results=2,
                    memory_tokens=1000,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="DEBUG",
                )
            )

            class Provider:
                calls = 0

                async def complete(
                    self,
                    _: object,
                    __: object,
                    tools: list[dict[str, object]],
                    **___: object,
                ) -> ProviderResponse:
                    self.calls += 1
                    if self.calls <= 3:
                        call = ToolCall(
                            f"bad-goal-{self.calls}",
                            "goal_create",
                            {
                                "title": "坏任务",
                                "success_criteria": "测试",
                                "next_action": "测试",
                                "next_review_at": "",
                            },
                        )
                    else:
                        self.assert_respond_only(tools)
                        call = ToolCall(
                            "failed-response",
                            "respond",
                            {
                                "delivery": "简短说明创建失败",
                                "messages": ["创建任务失败：缺少有效的执行时间。"],
                                "continuity": {
                                    "topic": "",
                                    "open_loops": [],
                                    "pending_commitments": [],
                                    "short_term_facts": [],
                                },
                                "mood": {"action": "keep"},
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

                @staticmethod
                def assert_respond_only(tools: list[dict[str, object]]) -> None:
                    if [tool["name"] for tool in tools] != ["respond"]:
                        raise AssertionError(tools)

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            event = IncomingMessage("qq:bad-goal", "bad-goal", "创建任务", 1, 1)
            daemon.store.add_event(event)
            with self.assertLogs("momoi.runtime.turns", level="DEBUG") as logs:
                await daemon._complete_batch_turn(
                    [event], asyncio.Event(), daemon._turn_id(event.event_id)
                )
            self.assertEqual(provider.calls, 4)
            self.assertIn("缺少有效的执行时间", daemon.store.due_outbox()[0].text)
            self.assertTrue(
                any("Invalid isoformat string" in message for message in logs.output)
            )
            daemon.store.close()

    async def test_due_goal_stays_ahead_of_queued_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                    system_prompt="test",
                    recent_raw_tokens=1000,
                    recent_turns=2,
                    memory_results=2,
                    memory_tokens=1000,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            daemon.autonomous.put_nowait(HEARTBEAT_QUEUE_ITEM)
            daemon.autonomous.put_nowait("goal-1")
            self.assertEqual(await daemon._next_work(), ("goal", "goal-1"))
            self.assertEqual(await daemon._next_work(), ("goal", HEARTBEAT_QUEUE_ITEM))
            daemon.store.close()

    async def test_goal_provider_failure_retries_same_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                    system_prompt="test",
                    recent_raw_tokens=1000,
                    recent_turns=2,
                    memory_results=2,
                    memory_tokens=1000,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            event = IncomingMessage("qq:1:retry", "retry", "稍后继续", 1, 1)
            daemon.store.add_event(event)
            draft = TurnDraft()
            goal = daemon.agenda_tools.execute(
                ToolCall(
                    "goal-retry",
                    "goal_create",
                    {
                        "title": "重试任务",
                        "success_criteria": "完成",
                        "next_action": "继续",
                        "next_review_at": (
                            datetime.now().astimezone() + timedelta(milliseconds=20)
                        ).isoformat(),
                    },
                ),
                draft,
                authority="owner",
                source_event_id=event.event_id,
                allow_notify=False,
            )["goal"]
            daemon.store.commit_turn([event], event.text, AgentReply(["好"]), draft)
            await asyncio.sleep(0.03)
            claimed = daemon.store.claim_due_goal()
            original_review = float(claimed["next_review_at"])
            turn_id = daemon._turn_id("goal", goal["id"], original_review)

            async def fail(_: str, __: str) -> None:
                raise ProviderError("temporary")

            daemon._complete_goal = fail  # type: ignore[method-assign]
            before = time.time()
            await daemon._complete_goal_turn(goal["id"], asyncio.Event())
            deferred = daemon.store.goal(goal["id"])
            self.assertEqual(deferred["next_review_at"], original_review)
            self.assertGreaterEqual(float(deferred["retry_at"]), before + 299)
            self.assertEqual(deferred["failure_count"], 1)
            self.assertIsNone(deferred["review_claimed_at"])
            turn = daemon.store._db.execute(
                "SELECT state, failure_reason FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            self.assertEqual(
                (turn["state"], turn["failure_reason"]),
                ("running", "ProviderError"),
            )

            daemon.store._db.execute(
                "UPDATE goals SET retry_at=?, review_claimed_at=NULL WHERE id=?",
                (time.time() - 1, goal["id"]),
            )
            daemon.store._db.commit()
            retried = daemon.store.claim_due_goal()
            self.assertEqual(
                daemon._turn_id("goal", goal["id"], retried["next_review_at"]),
                turn_id,
            )
            daemon.store.release_goal_claim(goal["id"], defer_seconds=900)
            stopped = daemon.store.goal(goal["id"])
            self.assertIsNone(stopped["retry_at"])
            self.assertEqual(stopped["failure_count"], 0)
            self.assertGreater(float(stopped["next_review_at"]), time.time() + 899)
            daemon.store.close()

    async def test_heartbeat_can_stay_silent_or_queue_a_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="test",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
                notifications=NotificationConfig(
                    timezone="Asia/Shanghai",
                    cooldown_seconds=0,
                    daily_budget=10,
                    pending_owner_delay_seconds=0,
                ),
                heartbeat=HeartbeatConfig(
                    enabled=True,
                    initial_delay_seconds=60,
                    min_interval_seconds=60,
                    max_interval_seconds=600,
                ),
            )
            daemon = MomoiDaemon(config)
            self.assertTrue((Path(directory) / "artifacts").is_dir())

            class Provider:
                calls = 0

                async def complete(
                    self,
                    _: object,
                    __: object,
                    tools: list[dict[str, object]],
                    **___: object,
                ) -> ProviderResponse:
                    self.calls += 1
                    names = {str(tool["name"]) for tool in tools}
                    if self.calls == 1:
                        expected = {
                            "memory_search",
                            "goal_create",
                            "curl",
                            "read_file",
                            "write_file",
                            "heartbeat_finish",
                        }
                        if names != expected:
                            raise AssertionError(names)
                        call = ToolCall(
                            "heartbeat-news",
                            "curl",
                            {"url": "https://news.example/today"},
                        )
                    elif self.calls == 3:
                        call = ToolCall(
                            "heartbeat-goal",
                            "goal_create",
                            {
                                "title": "继续整理关卡点子",
                                "success_criteria": "写下一份可玩的关卡草案",
                                "next_action": "把玩法联想整理成关卡结构",
                                "next_review_at": (
                                    datetime.now().astimezone() + timedelta(hours=1)
                                ).isoformat(),
                            },
                        )
                    else:
                        messages = (
                            [] if self.calls == 2 else ["刚想到一个关卡点子！"]
                        )
                        call = ToolCall(
                            f"heartbeat-{self.calls}",
                            "heartbeat_finish",
                            {
                                "messages": messages,
                                "activity": "整理小游戏关卡灵感",
                                "result": (
                                    "读完一条游戏新闻并记下玩法联想"
                                    if self.calls == 2
                                    else "已建立自己的关卡草案任务继续整理"
                                ),
                                "next_check_minutes": 2,
                                "reason": "有具体的新点子才分享",
                                "mood": {"action": "keep"},
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

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]

            async def read_news(_: ToolCall) -> dict[str, object]:
                return {"ok": True, "status": 200, "body": "新玩法公开"}

            daemon.builtin_tools.execute = read_news  # type: ignore[method-assign]
            daemon.store._db.execute(
                "UPDATE self_state SET next_heartbeat_at=? WHERE id=1",
                (time.time() - 1,),
            )
            daemon.store._db.commit()
            self.assertIsNotNone(
                daemon.store.claim_due_heartbeat(config.heartbeat, config.notifications)
            )
            await daemon._complete_heartbeat_turn(asyncio.Event())
            self.assertEqual(
                daemon.store._db.execute(
                    "SELECT COUNT(*) FROM notifications"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                daemon.store.self_state()["activity"], "整理小游戏关卡灵感"
            )
            self.assertEqual(
                daemon.store.self_state()["activity_result"],
                "读完一条游戏新闻并记下玩法联想",
            )

            daemon.store._db.execute(
                "UPDATE self_state SET next_heartbeat_at=? WHERE id=1",
                (time.time() - 1,),
            )
            daemon.store._db.commit()
            self.assertIsNotNone(
                daemon.store.claim_due_heartbeat(config.heartbeat, config.notifications)
            )
            await daemon._complete_heartbeat_turn(asyncio.Event())
            notification = daemon.store.claim_due_notification(config.notifications)
            self.assertIsNotNone(notification)
            self.assertTrue(
                daemon.store.queue_notification(
                    str(notification["id"]), config=config.notifications
                )
            )
            self.assertEqual(daemon.store.due_outbox()[0].text, "刚想到一个关卡点子！")
            goal = daemon.store.list_goals()[0]
            self.assertEqual(goal["authority"], "agent")
            self.assertEqual(goal["title"], "继续整理关卡点子")
            self.assertEqual(provider.calls, 4)
            daemon.store.close()

    async def test_owner_turn_stops_cleanly_at_configured_token_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
                turn_max_total_tokens=1,
            )
            daemon = MomoiDaemon(config)

            class Provider:
                calls = 0

                async def complete(self, *_: object, **__: object) -> ProviderResponse:
                    self.calls += 1
                    raise AssertionError("provider must not be called beyond budget")

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            event = IncomingMessage("qq:budget", "budget", "继续一个很长的任务", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            await daemon._complete_batch_turn([event], asyncio.Event(), turn_id)
            self.assertEqual(provider.calls, 0)
            self.assertIn("per-turn processing limit", daemon.store.due_outbox()[0].text)
            turn = daemon.store._db.execute(
                "SELECT state, llm_calls FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            self.assertEqual((turn["state"], turn["llm_calls"]), ("completed", 0))
            daemon.store.close()

    async def test_owner_turn_does_not_retry_after_provider_exhausts_retries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)

            class Provider:
                calls = 0

                async def complete(self, *_: object, **__: object) -> ProviderResponse:
                    self.calls += 1
                    raise ProviderError("model engine error")

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            event = IncomingMessage("qq:provider-error", "provider-error", "测试", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)

            await asyncio.wait_for(
                daemon._complete_batch_turn([event], asyncio.Event(), turn_id),
                timeout=1,
            )

            self.assertEqual(provider.calls, 1)
            self.assertIn("model service failed", daemon.store.due_outbox()[0].text)
            turn = daemon.store._db.execute(
                "SELECT state, failure_reason FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            self.assertEqual(
                (turn["state"], turn["failure_reason"]), ("completed", "ProviderError")
            )
            daemon.store.close()

    async def test_fatal_error_after_external_effect_requires_reconciliation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)
            event = IncomingMessage(
                "qq:fatal-after-tool", "fatal-after-tool", "测试", 1, 1
            )
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)

            async def fail_after_tool(_: object, current_turn_id: str) -> None:
                daemon.store.begin_tool_call(
                    current_turn_id, "call-1", "write_file", {}, "write"
                )
                raise RuntimeError("boom")

            daemon._complete_batch = fail_after_tool  # type: ignore[method-assign]
            await daemon._complete_batch_turn([event], asyncio.Event(), turn_id)

            self.assertIn(f"/resolve {turn_id[:12]}", daemon.store.due_outbox()[0].text)
            self.assertIn(turn_id, daemon.store.open_reconciliations_context())
            daemon.store.close()

    async def test_scheduler_queues_persisted_notification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
                notifications=NotificationConfig(
                    cooldown_seconds=0,
                    daily_budget=10,
                    urgent_daily_budget=10,
                    pending_owner_delay_seconds=0,
                ),
            )
            daemon = MomoiDaemon(config)
            daemon.store.commit_autonomous_turn(
                "goal",
                TurnDraft(
                    notification_messages=["后台检查完成"],
                    notification_key="check.result",
                    notification_reason="test",
                ),
                turn_id="notification-turn",
            )
            stop = asyncio.Event()
            worker = asyncio.create_task(daemon._scheduler_worker(stop))
            for _ in range(100):
                if daemon.store.due_outbox():
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(daemon.store.due_outbox()[0].text, "后台检查完成")
            stop.set()
            daemon.agenda_changed.set()
            await worker
            daemon.store.close()

    async def test_stop_command_cancels_active_turn_and_is_queued_for_llm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                recent_raw_tokens=1000,
                recent_turns=6,
                memory_results=6,
                memory_tokens=1000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)
            daemon._active_turn = asyncio.create_task(asyncio.sleep(3600))
            command = IncomingMessage("qq:1:stop", "stop", "/stop", 1, 1)
            await daemon._receive(command)
            with self.assertRaises(asyncio.CancelledError):
                await daemon._active_turn
            self.assertEqual((await daemon.incoming.get()).text, "/stop")
            self.assertEqual(daemon.store.pending_events()[0].text, "/stop")
            daemon.store.close()

    async def test_stop_cancels_autonomous_turn_and_defers_goal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 0.01, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)
            source = IncomingMessage("qq:1:goal-stop", "goal-stop", "继续任务", 1, 1)
            daemon.store.add_event(source)
            draft = TurnDraft()
            created = daemon.agenda_tools.execute(
                ToolCall(
                    "goal-stop-create",
                    "goal_create",
                    {
                        "title": "长任务",
                        "success_criteria": "完成",
                        "next_action": "继续执行",
                        "next_review_at": (
                            datetime.now().astimezone() + timedelta(milliseconds=20)
                        ).isoformat(),
                    },
                ),
                draft,
                authority="owner",
                source_event_id=source.event_id,
                allow_notify=False,
            )
            goal_id = created["goal"]["id"]
            daemon.store.commit_turn(
                [source], source.text, AgentReply(["接下了"]), draft
            )
            initial = daemon.store.due_outbox()[0]
            daemon.store.mark_sent(initial.id)
            time.sleep(0.03)
            daemon.store.claim_due_goal()

            started = asyncio.Event()

            class Provider:
                def __init__(self) -> None:
                    self.calls = 0

                async def complete(self, *_: object, **__: object) -> ProviderResponse:
                    self.calls += 1
                    if self.calls == 1:
                        started.set()
                        await asyncio.Future()
                    call = ToolCall(
                        "stop-response",
                        "respond",
                        {
                            "delivery": "直接确认已经停下",
                            "messages": ["已经停下来了"],
                            "continuity": {
                                "topic": "",
                                "open_loops": [],
                                "pending_commitments": [],
                                "short_term_facts": [],
                            },
                            "mood": {"action": "keep"},
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

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            daemon.autonomous.put_nowait(goal_id)
            worker = asyncio.create_task(daemon._agent_worker(asyncio.Event()))
            try:
                await asyncio.wait_for(started.wait(), timeout=1)
                await daemon._receive(
                    IncomingMessage("qq:1:stop-goal", "stop-goal", "/stop", 2, 2)
                )
                for _ in range(100):
                    if daemon.store.due_outbox():
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(daemon.store.due_outbox()[0].text, "已经停下来了")
                goal = daemon.store.goal(goal_id)
                self.assertGreater(float(goal["next_review_at"]), time.time() + 800)
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker
                daemon.store.close()

    async def test_stop_during_external_tool_leaves_ambiguous_audit_and_cancels_turn(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 0.01, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)

            async def blocked_write(_: ToolCall) -> dict[str, object]:
                await asyncio.sleep(30)
                return {"ok": True}

            daemon.builtin_tools.execute = blocked_write  # type: ignore[method-assign]

            class Provider:
                def __init__(self) -> None:
                    self.calls = 0

                async def complete(self, *_: object, **__: object) -> ProviderResponse:
                    self.calls += 1
                    if self.calls == 1:
                        call = ToolCall(
                            "blocked-write",
                            "write_file",
                            {"path": "/tmp/momoi-stop-test", "content": "test"},
                        )
                    else:
                        call = ToolCall(
                            "stop-after-tool",
                            "respond",
                            {
                                "delivery": "简短确认任务终止",
                                "messages": ["已经终止当前任务"],
                                "continuity": {
                                    "topic": "",
                                    "open_loops": [],
                                    "pending_commitments": [],
                                    "short_term_facts": [],
                                },
                                "mood": {"action": "keep"},
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

            daemon.provider = Provider()  # type: ignore[assignment]
            stop = asyncio.Event()
            worker = asyncio.create_task(daemon._agent_worker(stop))
            original = IncomingMessage("qq:1:tool-stop", "tool-stop", "等一会儿", 1, 1)
            try:
                await daemon._receive(original)
                for _ in range(100):
                    audit = daemon.store._db.execute(
                        "SELECT state FROM tool_audit WHERE tool_call_id='blocked-write'"
                    ).fetchone()
                    if audit is not None:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(audit["state"], "dispatching")
                await daemon._receive(
                    IncomingMessage("qq:1:stop-tool", "stop-tool", "/stop", 2, 2)
                )
                for _ in range(100):
                    row = daemon.store._db.execute(
                        "SELECT state FROM turns WHERE id=?",
                        (daemon._turn_id(original.event_id),),
                    ).fetchone()
                    if row is not None and row["state"] == "cancelled":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(row["state"], "cancelled")
                self.assertEqual(
                    daemon.store._db.execute(
                        "SELECT state FROM tool_audit WHERE tool_call_id='blocked-write'"
                    ).fetchone()["state"],
                    "dispatching",
                )
                self.assertIn(
                    daemon._turn_id(original.event_id),
                    daemon.store.open_reconciliations_context(),
                )
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker
                daemon.store.close()

    async def test_message_reaches_llm_and_reply_reaches_napcat(self) -> None:
        stop = asyncio.Event()
        sent: list[str] = []
        llm_requests: list[dict[str, object]] = []

        async def llm(request: web.Request) -> web.Response:
            llm_requests.append(await request.json())
            if len(llm_requests) == 1:
                return web.json_response(
                    {
                        "stop_reason": "tool_use",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "progress-1",
                                "name": "send_message",
                                "input": {
                                    "delivery": "自然告诉主人我已经开始处理",
                                    "messages": ["我先处理一下"],
                                },
                            }
                        ],
                    }
                )
            if len(llm_requests) <= 4:
                return web.json_response(
                    {
                        "stop_reason": "tool_use",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"search-{len(llm_requests)}",
                                "name": "memory_search",
                                "input": {"query": "问候"},
                            }
                        ],
                    }
                )
            if len(llm_requests) == 5:
                return web.json_response(
                    {"content": [{"type": "text", "text": "这段 raw text 不应发送"}]}
                )
            return web.json_response(
                {
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "respond-1",
                            "name": "respond",
                            "input": {
                                "delivery": "完成后简短收尾",
                                "messages": ["测试回复一", "测试回复二"],
                                "continuity": {
                                    "topic": "",
                                    "open_loops": [],
                                    "pending_commitments": [],
                                    "short_term_facts": [],
                                },
                                "mood": {"action": "keep"},
                            },
                        }
                    ],
                }
            )

        async def napcat(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": 10000,
                    "user_id": 20000,
                    "message_id": 1,
                    "time": 1,
                    "message": [{"type": "text", "data": {"text": "你好"}}],
                }
            )
            async for message in socket:
                payload = json.loads(message.data)
                sent.append(payload["params"]["message"][0]["data"]["text"])
                await socket.send_json(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"message_id": 2},
                        "echo": payload["echo"],
                    }
                )
                if len(sent) == 3:
                    stop.set()
            return socket

        llm_server = TestServer(web.Application())
        llm_server.app.router.add_post("/v1/messages", llm)
        napcat_server = TestServer(web.Application())
        napcat_server.app.router.add_get("/", napcat)
        await llm_server.start_server()
        await napcat_server.start_server()
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = AppConfig(
                    llm=LLMConfig(
                        base_url=str(llm_server.make_url("/")).rstrip("/"),
                        api_key="test",
                        model="test",
                        max_tokens=100,
                        temperature=0,
                        timeout_seconds=1,
                        max_retries=0,
                    ),
                channel=NapCatConfig(
                        url=str(napcat_server.make_url("/")).replace(
                            "http://", "ws://"
                        ),
                        owner_qq="20000",
                        quiet_seconds=0.01,
                        max_batch_seconds=1,
                        heartbeat_seconds=1,
                        reconnect_max_seconds=1,
                        send_timeout_seconds=1,
                    ),
                    system_prompt="You are Momoi.",
                    recent_raw_tokens=1000,
                    recent_turns=6,
                    memory_results=6,
                    memory_tokens=1000,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
                with patch("momoi.runtime.daemon.random.uniform", return_value=0):
                    await asyncio.wait_for(MomoiDaemon(config).run(stop), timeout=2)
        finally:
            await napcat_server.close()
            await llm_server.close()

        self.assertEqual(sent, ["我先处理一下", "测试回复一", "测试回复二"])
        self.assertEqual(len(llm_requests), 6)
        self.assertIn("tools", llm_requests[0])
        self.assertIn(
            "send_message", [tool["name"] for tool in llm_requests[0]["tools"]]
        )
        self.assertEqual(
            [tool["name"] for tool in llm_requests[5]["tools"]], ["respond"]
        )
        self.assertNotIn("tool_choice", llm_requests[5])
        self.assertEqual(
            llm_requests[0]["system"][0]["cache_control"], {"type": "ephemeral"}
        )
        self.assertIn("You are Momoi.", llm_requests[0]["system"][0]["text"])
        self.assertTrue(
            llm_requests[0]["system"][0]["text"].rstrip().endswith("You are Momoi.")
        )
        self.assertEqual(len(llm_requests[0]["system"]), 1)
        self.assertEqual(
            llm_requests[1]["messages"][-1]["content"][0]["type"], "tool_result"
        )
        self.assertEqual(llm_requests[0]["messages"][-1]["role"], "user")
        current_content = llm_requests[0]["messages"][-1]["content"]
        self.assertEqual(current_content[0]["cache_control"], {"type": "ephemeral"})
        current_text = current_content[0]["text"]
        self.assertIn("你好", current_text)
        self.assertIn("Trusted runtime context", current_text)
        self.assertIn(
            "Consecutive messages from the authenticated user",
            current_text,
        )
        self.assertNotIn("主人", current_text)
