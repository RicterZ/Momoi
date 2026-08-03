import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config import AppConfig, LLMConfig
from momoi.models import IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime import MomoiDaemon
from momoi.runtime.context_planner import (
    ContextPlanError,
    degraded_context_plan,
    parse_context_plan,
)
from momoi.runtime.turns import CONTEXT_PLANNER_SYSTEM_PROMPT


def app_config(directory: str) -> AppConfig:
    return AppConfig(
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


def response_plan() -> dict[str, object]:
    return {
        "version": 1,
        "intent_units": [
            {
                "id": "social",
                "event_ids": ["event-1"],
                "text": "刷微博",
                "intent": "browse social feed",
                "references": [],
                "recall_queries": ["owner recent Weibo interests"],
            },
            {
                "id": "mail",
                "event_ids": ["event-1"],
                "text": "看邮件",
                "intent": "check mail",
                "references": ["之前等的邮件"],
                "recall_queries": ["pending expected email thread"],
            },
        ],
        "episode_bindings": [
            {
                "episode_ref": "new:social",
                "title": "微博浏览",
                "relation": "primary",
                "unit_ids": ["social"],
                "topics": ["微博"],
                "entities": [],
                "open_loops": [],
                "salience": 0.4,
            },
            {
                "episode_ref": "new:mail",
                "title": "邮件跟进",
                "relation": "primary",
                "unit_ids": ["mail"],
                "topics": ["邮件"],
                "entities": [],
                "open_loops": ["等待中的邮件"],
                "salience": 0.8,
            },
        ],
        "episode_links": [
            {
                "from_episode_ref": "new:mail",
                "to_episode_ref": "new:social",
                "kind": "references",
            }
        ],
        "uncertainty": [],
    }


class ContextPlannerTest(unittest.TestCase):
    def test_parser_requires_event_coverage_and_normalizes_episode_refs(self) -> None:
        plan = response_plan()
        parsed = parse_context_plan(
            json.dumps(plan), ["event-1"], [], "turn-1", 1
        )
        bindings = parsed["episode_bindings"]
        self.assertEqual(len(bindings), 2)
        self.assertTrue(all(item["is_new"] for item in bindings))
        self.assertNotEqual(bindings[0]["episode_id"], bindings[1]["episode_id"])
        self.assertEqual(
            parsed["episode_links"][0]["from_episode_id"],
            bindings[1]["episode_id"],
        )

        plan["intent_units"][0]["event_ids"] = ["unknown"]
        with self.assertRaisesRegex(ContextPlanError, "unknown_event_id"):
            parse_context_plan(json.dumps(plan), ["event-1"], [], "turn-1", 1)

    def test_degraded_plan_splits_message_segments_and_marks_uncertainty(self) -> None:
        plan = degraded_context_plan(
            [{"event_id": "event-1", "channel": "napcat", "text": "先查邮件；再看微博。"}],
            "invalid_json",
        )
        self.assertEqual(len(plan["intent_units"]), 2)
        self.assertEqual(plan["episode_bindings"], [])
        self.assertIn("invalid_json", plan["uncertainty"][0])


class ContextPlannerAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_planner_runs_without_tools_before_main_and_commits_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(app_config(directory))

            class Provider:
                calls: list[str] = []

                async def complete(
                    provider_self,
                    system: object,
                    messages: list[dict[str, object]],
                    tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    if system == CONTEXT_PLANNER_SYSTEM_PROMPT:
                        provider_self.calls.append("planner")
                        self.assertEqual(tools, [])
                        payload = json.loads(str(messages[0]["content"]))
                        self.assertEqual(
                            payload["owner_messages"][0]["text"], "刷微博，也看下之前等的邮件"
                        )
                        return ProviderResponse(
                            [
                                {
                                    "type": "text",
                                    "text": json.dumps(response_plan(), ensure_ascii=False),
                                }
                            ],
                            [],
                        )
                    provider_self.calls.append("main")
                    self.assertIn("<context_plan>", json.dumps(messages, ensure_ascii=False))
                    self.assertEqual(daemon.store.list_episode_candidates(), [])
                    call = ToolCall(
                        "respond",
                        "respond",
                        {
                            "delivery": "回应两个事项",
                            "messages": ["我分开看了这两件事。"],
                            "expects_reply": False,
                            "reply_expectation": "",
                            "continuity": {
                                "topic": "微博与邮件",
                                "open_loops": [],
                                "pending_commitments": [],
                                "short_term_facts": [],
                            },
                            "mood": {"action": "keep"},
                        },
                    )
                    return ProviderResponse([], [call])

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            event = IncomingMessage(
                "event-1", "1", "刷微博，也看下之前等的邮件", 1, 1
            )
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            await daemon._complete_batch_turn([event], asyncio.Event(), turn_id)

            self.assertEqual(provider.calls, ["planner", "main"])
            stored = daemon.store.context_plan(turn_id)
            self.assertEqual(stored["state"], "planned")
            self.assertEqual(len(stored["plan"]["intent_units"]), 2)
            self.assertEqual(len(daemon.store.list_episode_candidates()), 2)
            self.assertEqual(
                daemon.store._db.execute(
                    "SELECT COUNT(*) FROM episode_turns WHERE turn_id=?", (turn_id,)
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                daemon.store._db.execute("SELECT COUNT(*) FROM episode_links").fetchone()[0],
                1,
            )
            daemon.store.close()

    async def test_invalid_plans_retry_once_then_degrade_before_main(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(app_config(directory))

            class Provider:
                calls = 0

                async def complete(
                    provider_self,
                    system: object,
                    messages: list[dict[str, object]],
                    tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    provider_self.calls += 1
                    if system == CONTEXT_PLANNER_SYSTEM_PROMPT:
                        if provider_self.calls == 2:
                            self.assertIn("invalid_json", str(messages[-1]["content"]))
                        return ProviderResponse(
                            [{"type": "text", "text": "not json"}], []
                        )
                    rendered = json.dumps(messages, ensure_ascii=False)
                    self.assertIn("degraded_message_segment", rendered)
                    self.assertIn("may miss references", rendered)
                    call = ToolCall(
                        "respond",
                        "respond",
                        {
                            "delivery": "正常回应并保留不确定性",
                            "messages": ["我先分别看邮件和微博。"],
                            "expects_reply": False,
                            "reply_expectation": "",
                            "continuity": {
                                "topic": "邮件与微博",
                                "open_loops": [],
                                "pending_commitments": [],
                                "short_term_facts": [],
                            },
                            "mood": {"action": "keep"},
                        },
                    )
                    return ProviderResponse([], [call])

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            event = IncomingMessage("event-degraded", "1", "先查邮件；再看微博。", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            await daemon._complete_batch_turn([event], asyncio.Event(), turn_id)

            self.assertEqual(provider.calls, 3)
            stored = daemon.store.context_plan(turn_id)
            self.assertEqual(stored["state"], "degraded")
            self.assertEqual(len(stored["plan"]["intent_units"]), 2)
            self.assertEqual(daemon.store.list_episode_candidates(), [])
            daemon.store.close()
