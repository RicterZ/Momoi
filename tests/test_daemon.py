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

from momoi.config import (
    AppConfig,
    HeartbeatConfig,
    LLMConfig,
    NapCatConfig,
    NotificationConfig,
)
from momoi.daemon import (
    HEARTBEAT_FINISH_SPEC,
    HEARTBEAT_QUEUE_ITEM,
    RESPOND_TOOL_SPEC,
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
from momoi.store import estimate_tokens


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
    async def test_repeated_invalid_tools_force_a_visible_failure_response(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    napcat=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
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
            with self.assertLogs("momoi.daemon", level="DEBUG") as logs:
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
                    napcat=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
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
                    napcat=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
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
                napcat=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
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
                    max_daily_turns=10,
                ),
            )
            daemon = MomoiDaemon(config)

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
                    self_names = [tool["name"] for tool in tools]
                    if self_names != ["heartbeat_finish"]:
                        raise AssertionError(self_names)
                    messages = [] if self.calls == 1 else ["刚想到一个关卡点子！"]
                    call = ToolCall(
                        f"heartbeat-{self.calls}",
                        "heartbeat_finish",
                        {
                            "messages": messages,
                            "activity": "整理小游戏关卡灵感",
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
            daemon.store.close()

    async def test_owner_turn_stops_cleanly_at_configured_token_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                napcat=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
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
            self.assertIn("达到单轮处理预算", daemon.store.due_outbox()[0].text)
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
                napcat=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
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
            self.assertIn("停止这个 Turn", daemon.store.due_outbox()[0].text)
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
                napcat=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
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

            self.assertIn("致命错误", daemon.store.due_outbox()[0].text)
            self.assertIn(turn_id, daemon.store.open_reconciliations_context())
            daemon.store.close()

    async def test_scheduler_queues_persisted_notification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                napcat=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
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
                napcat=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
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
                napcat=NapCatConfig("ws://127.0.0.1", "20000", 0.01, 60, 30, 30, 20),
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
                napcat=NapCatConfig("ws://127.0.0.1", "20000", 0.01, 60, 30, 30, 20),
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
                                "input": {"messages": ["我先处理一下"]},
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
                    napcat=NapCatConfig(
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
                with patch("momoi.daemon.random.uniform", return_value=0):
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
