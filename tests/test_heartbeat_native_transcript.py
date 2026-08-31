import copy
import tempfile
import unittest
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config import AppConfig, LLMConfig
from momoi.models import AgentReply, IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime import MomoiDaemon
from momoi.runtime.context_planner import degraded_heartbeat_plan


class HeartbeatNativeTranscriptTest(unittest.IsolatedAsyncioTestCase):
    async def test_execution_reads_shared_conversation_as_native_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="contract\n{{SOUL}}\n{{CAPABILITY_POLICIES}}",
                    recent_raw_tokens=1000,
                    recent_turns=2,
                    memory_results=2,
                    memory_tokens=1000,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            event = IncomingMessage("heartbeat:event", "1", "我到家了", 1, 1)
            daemon.store.add_event(event)
            owner_turn = daemon.store.commit_turn(
                [event],
                event.text,
                AgentReply(["终于回来了"]),
            )
            outbox_id = daemon.store._db.execute(
                "SELECT id FROM outbox WHERE turn_id=?", (owner_turn,)
            ).fetchone()["id"]
            daemon.store.mark_sent(int(outbox_id))

            async def plan(*_args: object, **_kwargs: object) -> dict[str, object]:
                return degraded_heartbeat_plan("resting", "test")

            class Provider:
                first_messages: list[dict[str, object]] = []

                async def complete(
                    self,
                    _system: object,
                    messages: list[dict[str, object]],
                    _tools: list[dict[str, object]],
                    **_kwargs: object,
                ) -> ProviderResponse:
                    self.first_messages = copy.deepcopy(messages)
                    call = ToolCall(
                        "finish",
                        "end_turn",
                        {
                            "expects_reply": False,
                            "reply_expectation": "",
                            "mood": {"decision": "unchanged"},
                            "heartbeat": {
                                "activity": "resting",
                                "result": "",
                                "next_check_minutes": 30,
                                "reason": "No activity was needed.",
                            },
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

            daemon._plan_heartbeat_context = plan  # type: ignore[method-assign]
            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            turn_id = daemon._turn_id("heartbeat-native")
            daemon.store.begin_turn(turn_id, "autonomous", [f"heartbeat:{turn_id}"])
            await daemon._complete_heartbeat(
                turn_id,
                owner_event_revision=int(
                    daemon.store.heartbeat_conversation_snapshot()[
                        "owner_event_revision"
                    ]
                ),
            )

            rendered = str(provider.first_messages)
            self.assertNotIn("<recent_turn_base>", rendered)
            self.assertNotIn("<recent_turn_append>", rendered)
            self.assertIn("<autonomous_heartbeat>", rendered)
            self.assertEqual(
                [message["role"] for message in provider.first_messages],
                ["user", "user", "assistant", "user"],
            )
            self.assertIn("我到家了", str(provider.first_messages[1]["content"]))
            self.assertIn("终于回来了", str(provider.first_messages[2]["content"]))
            daemon.store.close()


if __name__ == "__main__":
    unittest.main()
