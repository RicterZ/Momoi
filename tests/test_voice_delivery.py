import asyncio
import base64
import json
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from momoi.channel import SendRejected
from momoi.channel.napcat import NapCatChannel, NapCatConfig
from momoi.models import ToolCall, AgentReply, ProviderResponse, TurnDraft
from momoi.config.models import AppConfig, LLMConfig
from momoi.runtime import MomoiDaemon
from momoi.runtime.agent import TurnExecutionSpec
from momoi.tts import AudioOutput, TTSProvider, TTSError
from momoi.runtime.dispatch.delivery import OutboxWorker
from momoi.policies import DaemonPolicy
from momoi.runtime.transcript.building import build_groups
from momoi.runtime.agent.delivery import BubbleDelivery, DeliveryPolicy
from momoi.runtime.agent.harness import TurnHarness
from momoi.runtime.agent.tool_surface import ToolSurface
from momoi.runtime.tool_contracts.voice import SEND_VOICE_TOOL_SPEC
from momoi.storage import Store


class VoiceDeliveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.audio = AudioOutput(b"\x02#!SILK_V3\nfixture", "silk")
        self.channel = NapCatChannel(
            NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
        )
        self.store = Store(self.root / "momoi.sqlite3")
        self.addCleanup(self.store.close)
        self.store.begin_turn("voice-turn", "owner", [])
        self.changed = asyncio.Event()
        self.policy = DeliveryPolicy(SimpleNamespace(), self.store)
        self.delivery = BubbleDelivery(
            self.store,
            {self.channel.name: self.channel, "weixin": SimpleNamespace(name="weixin")},
            self.policy,
            self.changed,
        )
        self.provider = AsyncMock(spec=TTSProvider)
        self.provider.synthesize.return_value = self.audio
        self.delivery.tts_provider = self.provider
        self.text = "完整的一段话。\n\n这里也要讲出来。"
        self.context = dict(
            turn_id="voice-turn", stage="owner", round_number=1,
            delivery_channel=self.channel, response_required=True,
            heartbeat_turn=False, reply_followup_turn=False,
            heartbeat_owner_event_revision=None, heartbeat_notification_key="heartbeat.chat",
            previous_tool_name=None, previous_bubbles=None, previous_channel="",
        )

    async def dispatch(self, arguments=None, **context):
        return await self.delivery.dispatch_voice(
            ToolCall("voice-call", "send_voice", arguments if arguments is not None else {"text": self.text}),
            **{**self.context, **context},
        )

    async def test_napcat_sends_standalone_record_from_memory(self):
        self.channel._send_action = AsyncMock(return_value="message-id")
        self.assertEqual(await self.channel.send_voice(self.audio), "message-id")
        self.channel._send_action.assert_awaited_once_with(
            "send_private_msg",
            {"message": [{"type": "record", "data": {
                "file": "base64://" + base64.b64encode(self.audio.data).decode(),
            }}]},
        )

    async def test_text_is_synthesized_once_and_persisted_in_transcript(self):
        result = await self.dispatch()
        self.assertTrue(result.result["ok"])
        self.assertEqual(result.bubbles, [self.text])
        self.assertTrue((await self.dispatch()).result["ok"])
        self.provider.synthesize.assert_not_awaited()
        rows = self.store.due_outbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kind, "voice")
        self.assertEqual(rows[0].text, self.text)
        self.assertEqual(rows[0].payload, {"action": "voice"})
        self.assertIsNone(rows[0].media_path)
        self.store.commit_turn([], "owner", AgentReply([]), turn_id="voice-turn")
        worker, stop = self.worker()
        async def send(action, params):
            stop.set()
            return "sent"
        self.channel._send_action = AsyncMock(side_effect=send)
        await worker._outbox_worker(stop)
        self.provider.synthesize.assert_awaited_once_with(self.text)
        messages = [dict(row) for row in self.store._db.execute(
            "SELECT * FROM messages WHERE turn_id=? ORDER BY id", ("voice-turn",),
        )]
        self.assertEqual([row["content"] for row in messages if row["role"] == "assistant"], [self.text])
        groups = build_groups(messages)
        self.assertEqual([group.parts[0] for group in groups if group.role == "assistant"], [self.text])
        self.assertEqual((await self.dispatch({"text": "different"})).result["error"], "tool_call_id_conflict")

    async def test_unsupported_channel_and_missing_provider_do_not_synthesize(self):
        result = await self.dispatch(delivery_channel=SimpleNamespace(name="weixin"))
        self.assertEqual(result.result["error"], "voice_not_supported")
        self.provider.synthesize.assert_not_awaited()
        self.delivery.tts_provider = None
        self.assertEqual((await self.dispatch()).result["error"], "tts_not_configured")
        self.assertEqual(self.store.due_outbox(), [])

    async def test_invalid_arguments_do_not_queue(self):
        for arguments in ({}, {"text": " "}, {"text": 1}, {"file": str(self.audio)},
                          {"text": "hello", "channel": "napcat"}):
            with self.subTest(arguments=arguments):
                self.assertFalse((await self.dispatch(arguments)).result["ok"])
        self.provider.synthesize.assert_not_awaited()
        self.assertEqual(self.store.due_outbox(), [])
        self.assertFalse(self.changed.is_set())

    async def test_voice_rejects_stickers_before_synthesis(self):
        for text in ("emotion://happy", "老师你好 emotion://happy"):
            result = await self.dispatch({"text": text})
            self.assertEqual(result.result["error"], "voice_cannot_include_emotion")
        self.assertEqual(self.store.due_outbox(), [])
        self.provider.synthesize.assert_not_awaited()

    def worker(self, store=None):
        worker = OutboxWorker()
        worker.store = store or self.store
        worker.outbox_changed = self.changed
        worker.bubble_delivery = self.delivery
        worker._channel_for = lambda _: self.channel
        worker.daemon_policy = DaemonPolicy()
        worker.agenda_changed = asyncio.Event()
        return worker, asyncio.Event()

    async def test_pending_voice_survives_restart_without_audio_on_disk(self):
        await self.dispatch()
        reopened = Store(self.root / "momoi.sqlite3")
        self.addCleanup(reopened.close)
        worker, stop = self.worker(reopened)
        async def send(action, params):
            stop.set()
            self.assertTrue(params["message"][0]["data"]["file"].startswith("base64://"))
            return "sent"
        self.channel._send_action = AsyncMock(side_effect=send)
        await worker._outbox_worker(stop)
        self.provider.synthesize.assert_awaited_once_with(self.text)
        self.assertEqual(reopened.due_outbox(), [])
        self.assertFalse(list(self.root.glob("*.silk")))
        self.assertFalse(list(self.root.glob("*.mp3")))

    async def test_synthesis_failure_does_not_claim_channel_delivery(self):
        await self.dispatch()
        worker, stop = self.worker()
        original = self.store.mark_failed
        def fail(outbox_id, reason):
            original(outbox_id, reason)
            stop.set()
        self.store.mark_failed = fail
        self.provider.synthesize.side_effect = TTSError("failed")
        self.channel.send_voice = AsyncMock()
        await worker._outbox_worker(stop)
        self.channel.send_voice.assert_not_awaited()
        row = self.store._db.execute("SELECT state, last_error FROM outbox").fetchone()
        self.assertEqual(tuple(row), ("failed", "failed"))

    async def test_contact_policy_rejects_before_queueing(self):
        self.policy.heartbeat_contact_error = lambda *_: "heartbeat_contact_unavailable"
        result = await self.dispatch(heartbeat_turn=True, heartbeat_owner_event_revision=1)
        self.assertEqual(result.result["error"], "heartbeat_contact_unavailable")
        self.provider.synthesize.assert_not_awaited()
        self.assertEqual(self.store.due_outbox(), [])

    async def test_cancellation_during_synthesis_prevents_channel_send(self):
        await self.dispatch()
        worker, stop = self.worker()
        async def synthesize(text):
            self.store.cancel_pending_outbox(self.channel.name, "cancelled")
            stop.set()
            return self.audio
        self.provider.synthesize.side_effect = synthesize
        self.channel.send_voice = AsyncMock()
        await worker._outbox_worker(stop)
        self.channel.send_voice.assert_not_awaited()
        self.assertEqual(self.store._db.execute("SELECT state FROM outbox").fetchone()[0], "superseded")

    def test_disabled_voice_is_hidden_and_harness_rejects_it(self):
        surface = ToolSurface(SimpleNamespace(tool_specs=[], configs={}), {"napcat": self.channel})
        name = SEND_VOICE_TOOL_SPEC["name"]
        self.assertNotIn(name, {tool["name"] for tool in surface.conversation_specs()})
        self.assertNotIn(name, surface.owner_progress_tool_names())
        self.assertEqual(surface.mcp_server_groups(), {})
        for stage in ("owner", "heartbeat", "webhook", "reply_followup", "goal"):
            with self.subTest(stage=stage):
                permitted = surface.permitted_names(stage)
                self.assertNotIn(name, permitted)
                harness = TurnHarness.for_stage(stage, permitted_tool_names=permitted)
                if harness.spec.first_tool:
                    harness.accept(harness.spec.first_tool)
                self.assertEqual(
                    harness.validate([ToolCall("voice", name, {"text": self.text})]),
                    "tool_not_allowed",
                )

    def test_enabled_voice_is_available_and_counts_as_owner_progress(self):
        surface = ToolSurface(SimpleNamespace(tool_specs=[], configs={}), {"napcat": self.channel}, voice_enabled=True)
        self.assertIn("send_voice", {tool["name"] for tool in surface.conversation_specs()})
        for stage in ("owner", "heartbeat", "webhook", "reply_followup", "goal"):
            self.assertIn("send_voice", surface.permitted_names(stage))
        harness = TurnHarness.for_stage("owner", progress_tool_names=frozenset({"curl"}))
        harness.accept("recall")
        voice = ToolCall("v", "send_voice", {"text": self.text})
        work = ToolCall("w", "curl", {})
        self.assertIsNone(harness.validate([voice, work]))
        harness.observe_calls([voice])
        self.assertIsNone(harness.validate([work]))
        unsupported = ToolSurface(SimpleNamespace(tool_specs=[]), {"napcat": SimpleNamespace()}, voice_enabled=True)
        self.assertEqual(surface.conversation_specs(), unsupported.conversation_specs())

    async def test_voice_runs_through_chat_and_goal_workflows(self):
        for stage in ("owner", "heartbeat", "webhook", "reply_followup", "goal"):
            with self.subTest(stage=stage):
                config = AppConfig(
                    llm=LLMConfig("http://localhost", "test", "test", 100, 0, 1, 0),
                    channel=self.channel.config, system_prompt="test",
                    transcript_turns_min=4, transcript_turns_max=4,
                    episode_raw_tail_turns=2, memory_results=2,
                    database=self.root / f"{stage}.sqlite3", log_level="INFO",
                )
                daemon = MomoiDaemon(config, tts_provider=self.provider)
                try:
                    turn_id = f"voice-{stage}"
                    daemon.store.begin_turn(turn_id, stage, [])
                    daemon.store.pending_owner_reply = lambda: {"turn_id": "previous"}
                    recalled = {key: "" for key in ("recall_memories", "query_recall", "reflection_memories", "episodes")}
                    daemon.submit_owner_context = AsyncMock(return_value=recalled)
                    daemon.prepare_heartbeat_context = AsyncMock(return_value={"context": recalled})
                    calls = []
                    if stage == "owner":
                        calls.append(ToolCall("recall", "recall", {}))
                    elif stage == "heartbeat":
                        calls.append(ToolCall("begin", "heartbeat_begin", {"tool_groups": []}))
                    calls.append(ToolCall("voice", "send_voice", {"text": self.text}))
                    end = {"reply_wait": {"wait": False}, "mood": {"decision": "unchanged"}}
                    if stage == "owner":
                        end["activity"] = {"decision": "unchanged"}
                    if stage == "heartbeat":
                        end["heartbeat"] = {"activity": "resting", "result": "", "next_check_minutes": 30, "reason": "rest"}
                    calls.append(ToolCall("end", "autonomous_finish" if stage == "goal" else "end_turn", end))

                    async def complete(_system, _messages, tools, **kwargs):
                        self.assertIn("send_voice", {tool["name"] for tool in tools})
                        if stage == "reply_followup":
                            self.assertIsNone(kwargs.get("required_tool"))
                        self.assertTrue(calls, "unexpected protocol retry")
                        call = calls.pop(0)
                        return ProviderResponse([{
                            "type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments,
                        }], [call])

                    daemon.provider = SimpleNamespace(config=SimpleNamespace(api_format="anthropic"), complete=complete)
                    draft = TurnDraft()
                    if stage == "goal":
                        draft.goals["goal-id"] = {}
                    result = await daemon._run_tool_loop(
                        [], [{"role": "user", "content": "test"}], daemon.tool_surface.conversation_specs(), [], draft,
                        execution=TurnExecutionSpec(stage, goal_id="goal-id" if stage == "goal" else None,
                                                    permitted_tools=daemon.tool_surface.permitted_names(stage)),
                        source_event_id="test", turn_id=turn_id, delivery_channel=daemon.channel,
                    )
                    self.assertEqual(calls, [])
                    if stage == "goal":
                        self.assertEqual(draft.notification_messages, [{"action": "voice", "text": self.text}])
                        self.assertEqual(draft.notification_priority, "normal")
                        self.assertEqual(daemon.store.due_outbox(), [])
                        with daemon.store._db:
                            daemon.store._db.execute(
                                """INSERT INTO notifications
                                   (id, turn_id, goal_id, notification_key, priority, reason,
                                    messages_json, state, not_before, created_at, target_channel)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                                ("voice-notification", turn_id, "goal-id", draft.notification_key,
                                 draft.notification_priority, draft.notification_reason,
                                 json.dumps(draft.notification_messages), time.time(), time.time(), daemon.channel.name),
                            )
                            notification = daemon.store._db.execute("SELECT * FROM notifications").fetchone()
                            daemon.store._queue_notification_row(notification, time.time(), daemon.channel.name)
                        row = daemon.store.due_outbox()[0]
                        self.assertEqual((row.text, row.kind, row.media_path), (self.text, "voice", None))
                        self.assertEqual(row.payload, {"action": "voice"})
                    else:
                        self.assertIsInstance(result, AgentReply)
                        row = daemon.store.due_outbox()[0]
                        self.assertEqual((row.text, row.kind), (self.text, "voice"))
                finally:
                    daemon.store.close()

    async def test_unsupported_channel_retains_schema_but_harness_blocks_voice(self):
        config = AppConfig(
            llm=LLMConfig("http://localhost", "test", "test", 100, 0, 1, 0),
            channel=self.channel.config, system_prompt="test", transcript_turns_min=4,
            transcript_turns_max=4, episode_raw_tail_turns=2, memory_results=2,
            database=self.root / "blocked.sqlite3", log_level="INFO",
        )
        daemon = MomoiDaemon(config, tts_provider=self.provider)
        try:
            turn_id = "blocked-voice"
            daemon.store.begin_turn(turn_id, "webhook", [])
            tools = daemon.tool_surface.conversation_specs()
            rounds = 0
            async def complete(_system, messages, request_tools, **kwargs):
                nonlocal rounds
                rounds += 1
                self.assertEqual(request_tools, tools)
                if rounds == 1:
                    call = ToolCall("voice", "send_voice", {"text": self.text})
                else:
                    self.assertEqual(rounds, 2)
                    self.assertIn("tool_not_allowed", str(messages[-1]))
                    call = ToolCall("end", "end_turn", {
                        "reply_wait": {"wait": False}, "mood": {"decision": "unchanged"},
                    })
                return ProviderResponse([{
                    "type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments,
                }], [call])
            daemon.provider = SimpleNamespace(config=SimpleNamespace(api_format="anthropic"), complete=complete)
            await daemon._run_tool_loop(
                [], [{"role": "user", "content": "test"}], tools, [], TurnDraft(),
                execution=TurnExecutionSpec("webhook", permitted_tools=daemon.tool_surface.permitted_names("webhook")),
                source_event_id="test", turn_id=turn_id, delivery_channel=SimpleNamespace(name="weixin"),
            )
            self.assertEqual(daemon.store.due_outbox(), [])
            self.provider.synthesize.assert_not_awaited()
        finally:
            daemon.store.close()
