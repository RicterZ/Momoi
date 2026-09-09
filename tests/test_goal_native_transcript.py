from tests.support import provider_catalog
import asyncio
import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.models import (
    AgentReply,
    IncomingMessage,
    ProviderResponse,
    ToolCall,
    TurnDraft,
)
from momoi.runtime import MomoiDaemon


class GoalNativeTranscriptTest(unittest.IsolatedAsyncioTestCase):
    async def test_goal_reads_shared_conversation_as_native_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    providers=provider_catalog(
                        LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0)
                    ),
                    channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                    system_prompt="contract\n{{SOUL}}\n{{CAPABILITY_POLICIES}}",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            event = IncomingMessage("goal:event", "1", "继续检查", 1, 1)
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
                            datetime.now(ZoneInfo("UTC")) + timedelta(minutes=1)
                        ).isoformat(),
                    },
                ),
                draft,
                authority="agent",
                source_event_id=event.event_id,
            )
            goal_id = str(created["goal"]["id"])
            owner_turn = daemon.store.commit_turn(
                [event],
                event.text,
                AgentReply(["好"]),
                draft,
            )
            outbox_id = daemon.store._db.execute(
                "SELECT id FROM outbox WHERE turn_id=?", (owner_turn,)
            ).fetchone()["id"]
            daemon.store.mark_sent(int(outbox_id))

            # A previous Goal has finished, but its remaining bubbles are still
            # waiting for simulated typing. The next Goal must see all of them.
            daemon.store.begin_turn("previous-goal", "goal", ["goal:water"])
            with daemon.store._db:
                daemon.store._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at, source_event_ids_json,
                        delivery_state)
                       VALUES ('previous-goal', 'event', '贴纸包到站', ?, '[]', 'delivered')""",
                    (datetime.now(ZoneInfo("UTC")).timestamp(),),
                )
            daemon.store.queue_progress(
                "previous-goal", "water-bubbles", ["两点了", "记得喝水"], "napcat",
            )
            daemon.store.commit_autonomous_turn(
                "water", TurnDraft(), turn_id="previous-goal",
            )
            daemon.store.mark_sent(daemon.store.due_outbox()[0].id)

            class Provider:
                calls = 0
                first_system: object = None
                first_tools: list[dict[str, object]] = []
                first_messages: list[dict[str, object]] = []
                surfaces: list[list[str]] = []
                required_tools: list[object] = []

                async def complete(
                    self,
                    _system: object,
                    messages: list[dict[str, object]],
                    _tools: list[dict[str, object]],
                    **kwargs: object,
                ) -> ProviderResponse:
                    self.calls += 1
                    self.surfaces.append([str(tool["name"]) for tool in _tools])
                    self.required_tools.append(kwargs.get("required_tool"))
                    if self.calls == 1:
                        self.first_system = copy.deepcopy(_system)
                        self.first_tools = copy.deepcopy(_tools)
                        self.first_messages = copy.deepcopy(messages)
                        return ProviderResponse(
                            [{"type": "text", "text": "plain text is invalid"}], []
                        )
                    if self.calls == 2:
                        call = ToolCall("premature-finish", "end_turn", {})
                    elif self.calls == 3:
                        call = ToolCall(
                            "finish",
                            "goal_review",
                            {
                                    "status": "waiting",
                                    "waiting_for": "下一次检查",
                                    "result": "本次检查正常",
                                    "next_review_at": (
                                        datetime.now(ZoneInfo("UTC"))
                                        + timedelta(hours=1)
                                    ).isoformat(),
                            },
                        )
                    else:
                        call = ToolCall("commit", "end_turn", {})
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

            end_turn = next(tool for tool in provider.first_tools if tool["name"] == "end_turn")
            from momoi.runtime.tool_contracts.conversation import END_TURN_TOOL_SPEC
            self.assertEqual(end_turn["input_schema"], END_TURN_TOOL_SPEC["input_schema"])
            rendered = str(provider.first_messages)
            self.assertNotIn("<workflow_contract>", str(provider.first_system))
            self.assertIn("<workflow_contract>", rendered)
            self.assertNotIn("<recent_conversation>", rendered)
            self.assertNotIn("<recent_turns>", rendered)
            self.assertIn("<due_goal>", rendered)
            self.assertEqual(
                [message["role"] for message in provider.first_messages],
                ["user", "user", "assistant", "user", "assistant", "user"],
            )
            self.assertIn("继续检查", str(provider.first_messages[1]["content"]))
            self.assertIn("好", str(provider.first_messages[2]["content"]))
            self.assertIn('<event id="E', str(provider.first_messages[3]["content"]))
            self.assertIn("贴纸包到站", str(provider.first_messages[3]["content"]))
            self.assertNotIn("<recent_external_events>", rendered)
            previous_speech = str(provider.first_messages[4]["content"])
            self.assertIn("两点了", previous_speech)
            self.assertIn("记得喝水", previous_speech)
            self.assertEqual(previous_speech.count('delivery="queued"'), 1)
            self.assertEqual(provider.calls, 4)
            expected_surface = [
                str(tool["name"]) for tool in daemon.tool_surface.conversation_specs()
            ]
            self.assertTrue(
                all(surface == expected_surface for surface in provider.surfaces)
            )
            self.assertEqual(
                provider.required_tools,
                [None, None, None, None],
            )
            self.assertEqual(daemon.store.goal(goal_id)["status"], "waiting")
            self.assertEqual(
                daemon.store.goal(goal_id)["latest_result"], "本次检查正常"
            )
            daemon.store.close()


if __name__ == "__main__":
    unittest.main()
