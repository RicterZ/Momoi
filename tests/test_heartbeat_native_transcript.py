import copy
import tempfile
import unittest
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig, LLMConfig
from momoi.models import AgentReply, IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime import MomoiDaemon


class HeartbeatNativeTranscriptTest(unittest.IsolatedAsyncioTestCase):
    async def test_rest_recovers_from_text_without_requesting_a_message(self) -> None:
        for with_terminal in (False, True):
            with self.subTest(with_terminal=with_terminal), tempfile.TemporaryDirectory() as directory:
                daemon = MomoiDaemon(AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                ))
                self.addCleanup(daemon.store.close)
                case = self
                begin = ToolCall("begin", "heartbeat_begin", {
                    "activity": "resting", "mode": "rest", "recall_mode": "skip",
                    "recall_queries": [], "tool_groups": [], "strategy": [],
                })
                finish = ToolCall("finish", "end_turn", {
                    "reply_wait": {"wait": False}, "mood": {"decision": "unchanged"},
                    "heartbeat": {
                        "activity": "resting", "result": "", "next_check_minutes": 30,
                        "reason": "No activity was needed.",
                    },
                })

                class Provider:
                    calls = 0

                    async def complete(self, _system, messages, _tools, **_kwargs):
                        self.calls += 1
                        call = begin if self.calls == 1 else finish
                        content = [{"type": "tool_use", "id": call.id,
                                    "name": call.name, "input": call.arguments}]
                        if self.calls == 2:
                            text = {"type": "text", "text": "I will rest and end this turn."}
                            return ProviderResponse(
                                [text, *content] if with_terminal else [text],
                                [call] if with_terminal else [],
                            )
                        if self.calls == 3:
                            correction = str(messages[-1]["content"])
                            case.assertNotIn("send_bubbles", correction)
                            case.assertIn("native tool calls", correction)
                            case.assertIn(
                                "assistant text accompanied tool calls" if with_terminal
                                else "no native tool call was returned",
                                correction,
                            )
                        case.assertLessEqual(self.calls, 3)
                        return ProviderResponse(content, [call])

                provider = Provider()
                daemon.provider = provider
                turn_id = daemon._turn_id("heartbeat-text-recovery")
                daemon.store.begin_turn(turn_id, "heartbeat", [f"heartbeat:{turn_id}"])
                await daemon._complete_heartbeat(turn_id, owner_event_revision=0)
                self.assertEqual(provider.calls, 3)
                self.assertEqual(daemon.store._db.execute(
                    "SELECT COUNT(*) FROM outbox WHERE turn_id=?", (turn_id,),
                ).fetchone()[0], 0)

    async def test_execution_reads_shared_conversation_as_native_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="contract\n{{SOUL}}\n{{CAPABILITY_POLICIES}}",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
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

            class Provider:
                calls = 0
                first_system: object = None
                first_messages: list[dict[str, object]] = []
                first_tools: list[str] = []

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
                        self.first_tools = [str(tool["name"]) for tool in _tools]
                        call = ToolCall(
                            "begin",
                            "heartbeat_begin",
                            {
                                "activity": "resting",
                                "mode": "rest",
                                "recall_mode": "skip",
                                "recall_queries": [],
                                "tool_groups": [],
                                "strategy": [],
                            },
                        )
                    else:
                        call = ToolCall(
                            "finish",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
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

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            turn_id = daemon._turn_id("heartbeat-native")
            daemon.store.begin_turn(turn_id, "heartbeat", [f"heartbeat:{turn_id}"])
            await daemon._complete_heartbeat(
                turn_id,
                owner_event_revision=int(
                    daemon.store.heartbeat_conversation_snapshot()[
                        "owner_event_revision"
                    ]
                ),
            )

            rendered = str(provider.first_messages)
            self.assertNotIn("Autonomous heartbeat contract", str(provider.first_system))
            self.assertIn("<workflow_contract>", rendered)
            self.assertIn("Autonomous heartbeat contract", rendered)
            self.assertNotIn("<recent_turn_base>", rendered)
            self.assertNotIn("<recent_turn_append>", rendered)
            self.assertIn("<autonomous_heartbeat>", rendered)
            self.assertNotIn("<heartbeat_plan>", rendered)
            self.assertIn("heartbeat_begin", provider.first_tools)
            self.assertIn("apply_patch", provider.first_tools)
            self.assertIn("delete_file", provider.first_tools)
            self.assertIn("sleep", provider.first_tools)
            self.assertEqual(
                provider.first_tools,
                [
                    str(tool["name"])
                    for tool in daemon.tool_surface.conversation_specs()
                ],
            )
            self.assertEqual(provider.calls, 2)
            self.assertEqual(
                [message["role"] for message in provider.first_messages],
                ["user", "user", "assistant", "user"],
            )
            self.assertIn("我到家了", str(provider.first_messages[1]["content"]))
            self.assertIn("终于回来了", str(provider.first_messages[2]["content"]))
            daemon.store.close()

    async def test_selected_mcp_group_is_resident_and_callable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )

            class MCP:
                tool_specs = [
                    {
                        "name": "mcp__demo__read",
                        "description": "Read demo state.",
                        "input_schema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    }
                ]
                configs = {"demo": {"description": "Demo tools."}}

                @staticmethod
                def has_tool(name: str) -> bool:
                    return name == "mcp__demo__read"

                @staticmethod
                def capability(_: str) -> str:
                    return "read"

                @staticmethod
                async def call(_: str, __: dict[str, object]) -> dict[str, object]:
                    return {"ok": True, "value": "dynamic heartbeat tool works"}

            daemon.mcp = MCP()  # type: ignore[assignment]
            daemon.tool_surface.mcp = daemon.mcp
            daemon.tool_executor.mcp = daemon.mcp
            case = self

            class Provider:
                calls = 0
                surfaces: list[list[str]] = []

                async def complete(
                    self,
                    _system: object,
                    messages: list[dict[str, object]],
                    tools: list[dict[str, object]],
                    **_kwargs: object,
                ) -> ProviderResponse:
                    self.calls += 1
                    names = [str(tool["name"]) for tool in tools]
                    self.surfaces.append(names)
                    if self.calls == 1:
                        case.assertNotIn("mcp__demo__read", names)
                        begin = next(
                            tool for tool in tools if tool["name"] == "heartbeat_begin"
                        )
                        groups = begin["input_schema"]["properties"]["tool_groups"]
                        case.assertEqual(groups["items"]["enum"], ["demo"])
                        call = ToolCall(
                            "begin",
                            "heartbeat_begin",
                            {
                                "activity": "inspect demo state",
                                "mode": "work",
                                "recall_mode": "search",
                                "recall_queries": [
                                    {
                                        "semantic": "Previous demo state observations",
                                        "keywords": ["demo"],
                                    }
                                ],
                                "tool_groups": ["demo"],
                                "strategy": [
                                    "Read current state",
                                    "Record the verified outcome",
                                ],
                            },
                        )
                    elif self.calls == 2:
                        case.assertIn("mcp__demo__read", names)
                        case.assertIn('"state": "started"', str(messages[-1]))
                        call = ToolCall("read-demo", "mcp__demo__read", {})
                    else:
                        case.assertIn("mcp__demo__read", names)
                        case.assertIn("dynamic heartbeat tool works", str(messages[-1]))
                        call = ToolCall(
                            "finish",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
                                "heartbeat": {
                                    "activity": "inspect demo state",
                                    "result": "planning complete",
                                    "next_check_minutes": 30,
                                    "reason": "test",
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

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            turn_id = daemon._turn_id("heartbeat-groups")
            daemon.store.begin_turn(turn_id, "heartbeat", [f"heartbeat:{turn_id}"])
            await daemon._complete_heartbeat(
                turn_id,
                owner_event_revision=0,
            )
            self.assertEqual(provider.calls, 3)
            self.assertNotEqual(provider.surfaces[0], provider.surfaces[1])
            self.assertEqual(provider.surfaces[1], provider.surfaces[2])
            daemon.store.close()


if __name__ == "__main__":
    unittest.main()
