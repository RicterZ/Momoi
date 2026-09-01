import asyncio
import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config import AppConfig, LLMConfig
from momoi.models import AgentReply, IncomingMessage, ProviderResponse, ToolCall, TurnDraft
from momoi.runtime import MomoiDaemon


class GoalNativeTranscriptTest(unittest.IsolatedAsyncioTestCase):
    async def test_goal_reads_shared_conversation_as_native_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="contract\n{{SOUL}}\n{{CAPABILITY_POLICIES}}",
                    recent_raw_tokens=1000,
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    memory_tokens=1000,
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
                            datetime.now().astimezone() + timedelta(minutes=1)
                        ).isoformat(),
                    },
                ),
                draft,
                authority="agent",
                source_event_id=event.event_id,
                allow_notify=False,
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

            class Provider:
                calls = 0
                first_system: object = None
                first_messages: list[dict[str, object]] = []

                async def complete(
                    self,
                    _system: object,
                    messages: list[dict[str, object]],
                    _tools: list[dict[str, object]],
                    **_kwargs: object,
                ) -> ProviderResponse:
                    self.calls += 1
                    if self.calls == 1:
                        self.first_system = copy.deepcopy(_system)
                        self.first_messages = copy.deepcopy(messages)
                        call = ToolCall(
                            "update",
                            "goal_update",
                            {
                                "goal_id": goal_id,
                                "status": "waiting",
                                "waiting_for": "下一次检查",
                                "latest_result": "本次检查正常",
                                "next_review_at": (
                                    datetime.now().astimezone()
                                    + timedelta(hours=1)
                                ).isoformat(),
                            },
                        )
                    else:
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

            rendered = str(provider.first_messages)
            self.assertNotIn("Due Goal contract", str(provider.first_system))
            self.assertIn("<workflow_contract>", rendered)
            self.assertIn("Due Goal contract", rendered)
            self.assertNotIn("<recent_conversation>", rendered)
            self.assertNotIn("<recent_turns>", rendered)
            self.assertIn("<due_goal>", rendered)
            self.assertEqual(
                [message["role"] for message in provider.first_messages],
                ["user", "user", "assistant", "user"],
            )
            self.assertIn("继续检查", str(provider.first_messages[1]["content"]))
            self.assertIn("好", str(provider.first_messages[2]["content"]))
            daemon.store.close()


if __name__ == "__main__":
    unittest.main()
