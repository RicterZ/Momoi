from tests.support import provider_catalog
import copy
import tempfile
import unittest
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.models import AgentReply, IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime import MomoiDaemon


class HeartbeatNativeTranscriptTest(unittest.IsolatedAsyncioTestCase):
    async def test_send_waits_for_successful_targeted_recall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(AppConfig(
                providers=provider_catalog(LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0)),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="test",
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_unsummarized_tail_turns=2,
                memory_results=2,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
                turn_max_protocol_retries=5,
            ))
            self.addCleanup(daemon.store.close)
            recall_arguments = {"units": [{
                "intent": "Share a specific show update",
                "recall_mode": "search",
                "recall_queries": [{"semantic": "Prior discussion of this show", "keywords": []}],
                "recall_from_turn_id": "",
            }]}
            calls = [
                ToolCall("begin", "heartbeat_begin", {
                    "activity": "read news", "mode": "work",
                    "strategy": ["Read current news"],
                }),
                ToolCall("early", "send_bubbles", {"bubbles": ["A show update"]}),
                ToolCall("bad-recall", "recall", recall_arguments),
                ToolCall("still-early", "send_bubbles", {"bubbles": ["A show update"]}),
                ToolCall("good-recall", "recall", recall_arguments),
                ToolCall("send", "send_bubbles", {"bubbles": ["A show update"]}),
                ToolCall("activity", "heartbeat_activity", {
                    "activity": "read news", "result": "Shared one update",
                    "next_check_minutes": 30, "reason": "Enough for now",
                }),
                ToolCall("finish", "end_turn", {
                    "reply_wait": {"wait": False}, "mood": {"decision": "unchanged"},
                }),
            ]
            recall_count = 0

            async def prepare_context(arguments):
                nonlocal recall_count
                recall_count += 1
                self.assertEqual(arguments["recall_mode"], "search")
                if recall_count == 1:
                    raise ValueError("retrieval unavailable")
                return {"memory_snapshots": {}, "context": {
                    "recall_memories": [], "query_recall": "no prior match",
                    "reflection_memories": [], "episodes": [],
                }}

            daemon.prepare_heartbeat_context = prepare_context
            case = self

            class Provider:
                calls = 0

                async def complete(self, _system, messages, _tools, **_kwargs):
                    call = calls[self.calls]
                    if self.calls == 2:
                        case.assertIn("heartbeat_recall_required_before_send", str(messages[-1]))
                    if self.calls == 4:
                        case.assertIn("heartbeat_recall_required_before_send", str(messages[-1]))
                    self.calls += 1
                    return ProviderResponse(
                        [{"type": "tool_use", "id": call.id, "name": call.name,
                          "input": call.arguments}], [call],
                    )

            provider = Provider()
            daemon.provider = provider
            turn_id = daemon._turn_id("heartbeat-recall-gate")
            daemon.store.begin_turn(turn_id, "heartbeat", [f"heartbeat:{turn_id}"])
            await daemon._complete_heartbeat(turn_id, owner_event_revision=0)
            self.assertEqual(provider.calls, len(calls))
            self.assertEqual(recall_count, 2)
            self.assertIsNone(daemon.store.context_plan(turn_id))
            self.assertEqual(daemon.store._db.execute(
                "SELECT COUNT(*) FROM outbox WHERE turn_id=?", (turn_id,),
            ).fetchone()[0], 1)

    async def test_rest_retries_undelivered_text_then_finishes_silently(self) -> None:
        for with_terminal in (False, True):
            with self.subTest(with_terminal=with_terminal), tempfile.TemporaryDirectory() as directory:
                daemon = MomoiDaemon(AppConfig(
                    providers=provider_catalog(LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0)),
                    channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_unsummarized_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                ))
                self.addCleanup(daemon.store.close)
                case = self
                begin = ToolCall("begin", "heartbeat_begin", {
                    "activity": "resting", "mode": "rest", "strategy": [],
                })
                finish = ToolCall("finish", "end_turn", {
                    "reply_wait": {"wait": False}, "mood": {"decision": "unchanged"},
                })

                class Provider:
                    calls = 0

                    async def complete(self, _system, messages, _tools, **_kwargs):
                        self.calls += 1
                        call = begin if self.calls == 1 else finish
                        if self.calls == 3:
                            call = ToolCall("activity", "heartbeat_activity", {"activity": "resting", "result": "", "next_check_minutes": 30, "reason": "No activity was needed."})
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
                            if with_terminal:
                                case.assertIn("heartbeat_activity_required_before_end_turn", correction)
                            else:
                                case.assertNotIn("send_bubbles", correction)
                        case.assertLessEqual(self.calls, 4)
                        return ProviderResponse(content, [call])

                provider = Provider()
                daemon.provider = provider
                turn_id = daemon._turn_id("heartbeat-text-recovery")
                daemon.store.begin_turn(turn_id, "heartbeat", [f"heartbeat:{turn_id}"])
                await daemon._complete_heartbeat(turn_id, owner_event_revision=0)
                self.assertEqual(provider.calls, 4)
                self.assertEqual(daemon.store._db.execute(
                    "SELECT COUNT(*) FROM outbox WHERE turn_id=?", (turn_id,),
                ).fetchone()[0], 0)

    async def test_execution_reads_shared_conversation_as_native_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    providers=provider_catalog(LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0)),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="contract\n{{SOUL}}",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_unsummarized_tail_turns=2,
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
            heartbeat_ids = []
            for index in range(2):
                past_turn = f"past-heartbeat-{index}"
                daemon.store.begin_turn(past_turn, "heartbeat", [f"heartbeat:{past_turn}"])
                daemon.store.commit_heartbeat(
                    past_turn,
                    owner_event_revision=0,
                    notification_config=daemon.config.notifications,
                    activity=f"历史活动{index}",
                    result=f"历史结果{index}",
                    next_heartbeat_at=0,
                    mood_update=None,
                    messages=[],
                    reason="test",
                )
                record = daemon.store._db.execute(
                    "SELECT id FROM messages WHERE turn_id=? AND delivery_state='internal'",
                    (past_turn,),
                ).fetchone()
                heartbeat_ids.append(f"H{record['id']}")

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
                                "strategy": [],
                            },
                        )
                    elif self.calls == 2:
                        call = ToolCall("activity", "heartbeat_activity", {"activity": "resting", "result": "", "next_check_minutes": 30, "reason": "No activity was needed."})
                    else:
                        call = ToolCall(
                            "finish",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
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
            self.assertNotIn("<workflow_contract>", str(provider.first_system))
            self.assertIn("<workflow_contract>", rendered)
            self.assertNotIn("<recent_turn_base>", rendered)
            self.assertNotIn("<recent_turn_append>", rendered)
            self.assertIn("<autonomous_heartbeat>", rendered)
            self.assertNotIn("<heartbeat_plan>", rendered)
            self.assertIn("heartbeat_begin", provider.first_tools)
            self.assertIn("apply_patch", provider.first_tools)
            self.assertIn("delete_file", provider.first_tools)
            self.assertNotIn("sleep", provider.first_tools)
            self.assertEqual(
                provider.first_tools,
                [
                    str(tool["name"])
                    for tool in daemon.tool_surface.conversation_specs()
                ],
            )
            self.assertEqual(provider.calls, 3)
            self.assertIn("<recent_episodes>", str(provider.first_messages[1]["content"]))
            self.assertEqual(
                [message["role"] for message in provider.first_messages],
                ["user", "user", "user"],
            )
            self.assertNotIn("我到家了", rendered)
            self.assertNotIn("终于回来了", rendered)
            latest = provider.first_messages[-1]["content"][0]["text"]
            self.assertNotIn("<recent_heartbeats>", latest)
            self.assertNotIn("recent_heartbeat_activities", rendered)
            for index, identifier in enumerate(heartbeat_ids):
                self.assertNotIn(f'<heartbeat id="{identifier}"', rendered)
                self.assertNotIn(f"Activity: 历史活动{index}", rendered)
                self.assertNotIn(f"Result: 历史结果{index}", rendered)
                self.assertNotIn(f"Activity: 历史活动{index}", latest)
            self.assertNotIn("<heartbeat id=", str(provider.first_messages[0]["content"]))
            daemon.store.close()

    async def test_searched_mcp_tool_is_resident_and_callable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    providers=provider_catalog(LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0)),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_unsummarized_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )

            class MCP:
                @staticmethod
                def tool_group(_: str) -> str:
                    return "demo"

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
                        case.assertNotIn("tool_groups", begin["input_schema"]["properties"])
                        case.assertIn("tool_search", names)
                        call = ToolCall(
                            "begin",
                            "heartbeat_begin",
                            {
                                "activity": "inspect demo state",
                                "mode": "work",
                                "strategy": [
                                    "Read current state",
                                    "Record the verified outcome",
                                ],
                            },
                        )
                    elif self.calls == 2:
                        case.assertNotIn("mcp__demo__read", names)
                        case.assertIn('"state": "started"', str(messages[-1]))
                        call = ToolCall("search-demo", "tool_search", {"query": "mcp__demo__read", "limit": 1})
                    elif self.calls == 3:
                        case.assertNotIn("mcp__demo__read", names)
                        case.assertIn("mcp__demo__read", str(messages[-1]))
                        call = ToolCall("enable-demo", "tool_enable", {"tools": ["mcp__demo__read"]})
                    elif self.calls == 4:
                        case.assertIn("mcp__demo__read", names)
                        call = ToolCall("read-demo", "mcp__demo__read", {})
                    elif self.calls == 5:
                        case.assertIn("mcp__demo__read", names)
                        case.assertIn("dynamic heartbeat tool works", str(messages[-1]))
                        call = ToolCall("activity", "heartbeat_activity", {"activity": "inspect demo state", "result": "planning complete", "next_check_minutes": 30, "reason": "test"})
                    else:
                        call = ToolCall(
                            "finish",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
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
            self.assertEqual(provider.calls, 6)
            self.assertEqual(provider.surfaces[0], provider.surfaces[1])
            self.assertEqual(provider.surfaces[1], provider.surfaces[2])
            self.assertNotEqual(provider.surfaces[2], provider.surfaces[3])
            self.assertEqual(provider.surfaces[3], provider.surfaces[4])
            self.assertEqual(provider.surfaces[4], provider.surfaces[5])
            daemon.store.close()


if __name__ == "__main__":
    unittest.main()
