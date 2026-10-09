from tests.support import provider_catalog
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.models import AgentReply, IncomingMessage, OwnerInputStatus, MessageRecalled, MessagePoked
from momoi.runtime import MomoiDaemon
from momoi.integrations.contracts.tts import AudioOutput


class OutboxInterruptTest(unittest.IsolatedAsyncioTestCase):
    async def test_call_interrupt_preserves_reason_in_owner_turn_and_log(self):
        from momoi.channel.napcat.voice_call.channel import QQCallChannel
        from types import SimpleNamespace

        channel = QQCallChannel(SimpleNamespace(), '123', object(), tts_enabled=True)
        channel.session_id = 'call'
        channel.status = {'phase': 'connected'}
        self.daemon.channels['qq_call'] = channel
        for reason in ('call_ended', 'owner_speech'):
            with self.subTest(reason=reason):
                entered = asyncio.Event()
                stop = asyncio.Event()
                message = IncomingMessage(reason, reason, '我要继续工作', 1, 1,
                    channel='qq_call', delivery_context=channel.routing_context())
                turn_id = self.daemon._turn_id(message.event_id)

                async def complete(batch, _, sealed_turn_id, target):
                    self.store.begin_turn(sealed_turn_id, 'owner', [message.event_id])
                    entered.set()
                    await asyncio.Event().wait()

                self.daemon._complete_batch_turn = complete
                self.daemon.incoming.put_nowait(message)
                worker = asyncio.create_task(self.daemon._agent_worker(stop))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    with self.assertLogs('momoi.runtime.daemon', level='INFO') as logs:
                        self.daemon._interrupt_qq_call(reason)
                        stop.set()
                        await asyncio.wait_for(worker, 1)
                    self.assertEqual(logs.records[-1].momoi_fields['reason'], reason)
                    row = self.store._db.execute(
                        'SELECT state, failure_reason FROM turns WHERE id=?', (turn_id,)
                    ).fetchone()
                    self.assertEqual(tuple(row), ('cancelled', reason))
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.daemon = MomoiDaemon(AppConfig(
            providers=provider_catalog(LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0)),
            channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
            system_prompt="test",
            transcript_turns_min=4,
            transcript_turns_max=4,
            episode_unsummarized_tail_turns=2,
            memory_results=2,
            database=Path(directory.name) / "momoi.sqlite3",
            log_level="INFO",
        ))
        self.store = self.daemon.store
        self.addCleanup(self.store.close)
        self.message = IncomingMessage("new-message", "1", "等一下", 1, 1)

    async def test_replyer_poke_outbox_replay_and_delivery(self):
        self.daemon.channel.poke_owner = AsyncMock()
        self.store.begin_turn("poke-turn", "owner", [])
        for _ in range(2):
            self.store.queue_progress("poke-turn", "reply-once", ["qq://poke"], "napcat")
        rows = self.store.due_outbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kind, "poke")
        self.assertEqual(rows[0].payload, {"action": "poke"})
        stop = asyncio.Event()
        async def poke():
            stop.set()
        self.daemon.channel.poke_owner.side_effect = poke
        self.daemon.channel._request_action = AsyncMock()
        await asyncio.wait_for(self.daemon._outbox_worker(stop), 1)
        self.daemon.channel.poke_owner.assert_awaited_once()
        self.daemon.channel._request_action.assert_not_awaited()
        self.assertEqual(self.store._db.execute("SELECT state FROM outbox").fetchone()[0], "sent")
        self.assertEqual(self.store.committed_reply("poke-turn", "reply-once")["bubbles"], ["qq://poke"])

    async def test_owner_poke_reaches_planner_without_cancelling_pending_text(self):
        self.store.queue_progress("previous", "previous", ["pending reply"], "napcat")
        await self.daemon._receive(MessagePoked("owner-poke", 2, "napcat", "owner", "assistant"))
        update = self.daemon.incoming.get_nowait()
        self.assertEqual(update.delivery_context["channel_notice"], "poke")
        self.assertTrue(self.daemon._owner_message_changed.is_set())
        self.assertEqual([row.text for row in self.store.due_outbox()], ["pending reply"])
        await self.daemon._receive(MessagePoked("bot-poke", 3, "napcat", "assistant", "owner"))
        self.assertTrue(self.daemon.incoming.empty())
        rows = self.store.recent_conversation_messages(10, 10000)
        self.assertEqual([row["content"] for row in rows if row["role"] == "event"],
                         ["【QQ 戳一戳】\n用户戳了机器人一下。", "【QQ 戳一戳】\n机器人戳了用户一下。"])

    async def test_idle_recall_is_offered_to_planner_as_runtime_event(self):
        message = IncomingMessage("recall-source", "1", "synthetic request", 1, 1, channel="napcat")
        await self.daemon._receive(message)
        self.daemon.incoming.get_nowait()
        await self.daemon._receive(MessageRecalled("recall-new", "1", 2, "napcat"))
        update = self.daemon.incoming.get_nowait()
        self.assertEqual(update.delivery_context["channel_notice"], "message_recall")
        self.assertFalse(self.daemon._message_current(message))
        self.assertTrue(self.store.message_recalled("napcat", "1"))
        self.assertEqual(self.store.pending_events(), [])

    async def test_recall_interrupts_active_owner_request_with_runtime_event(self):
        entered = asyncio.Event()
        async def active():
            entered.set()
            await asyncio.Event().wait()
        self.daemon.start_active_turn(active(), stage="owner", channel="napcat")
        try:
            await entered.wait()
            await self.daemon._receive(MessageRecalled("active-recall", "1", 2, "napcat"))
            update = self.daemon.incoming.get_nowait()
            self.assertEqual(update.delivery_context["channel_notice"], "message_recall")
            self.assertTrue(self.daemon._owner_message_changed.is_set())
        finally:
            self.daemon._active_turn.cancel()
            await asyncio.gather(self.daemon._active_turn, return_exceptions=True)
            self.daemon.finish_active_turn()

    async def test_text_gap_sets_typing_and_clears_it_before_send(self):
        self.store.queue_progress("typing", "typing", ["first", "second"], "napcat")
        stop = asyncio.Event()
        actions = []
        async def typing(active):
            actions.append(("typing", active))
        async def send(payload):
            text = payload["segments"][0]["data"]["text"]
            actions.append(("send", text))
            if text == "second":
                stop.set()
            return "123"
        self.daemon.channel.set_typing = typing
        self.daemon.channel.send_message = send
        with patch("momoi.runtime.dispatch.delivery.random.uniform", return_value=0.001):
            await asyncio.wait_for(self.daemon._outbox_worker(stop), 1)
        self.assertEqual(actions, [("send", "first"), ("typing", True),
                                   ("typing", False), ("send", "second")])

    async def test_new_message_cancels_only_existing_queue_on_its_channel(self):
        self.store.commit_turn(
            [], "", AgentReply(["已发送", "未发送"]),
            turn_id="old", target_channel="napcat",
        )
        sent = self.store.due_outbox()[0]
        self.store.mark_sending(sent.id)
        self.store.mark_sent(sent.id)
        self.store.queue_progress("other", "other", ["其他渠道"], "weixin")

        await self.daemon._receive(self.message)
        self.assertEqual([tuple(row) for row in self.store._db.execute(
            "SELECT text, state FROM outbox ORDER BY id"
        )], [("已发送", "sent"), ("未发送", "superseded"), ("其他渠道", "pending")])
        self.assertEqual([tuple(row) for row in self.store._db.execute(
            "SELECT content, delivery_state FROM messages WHERE role='assistant' ORDER BY id"
        )], [("已发送", "delivered"), ("未发送", "failed")])
        self.assertFalse(self.store.mark_sending(sent.id + 1))

        self.store.queue_progress("new", "new", ["新的回复"], "napcat")
        await self.daemon._receive(self.message)  # Transport redelivery is not new input.
        await self.daemon._receive(OwnerInputStatus(channel="napcat"))
        self.assertEqual(
            [row.text for row in self.store.due_outbox()], ["其他渠道", "新的回复"]
        )

    async def test_new_message_wakes_gap_and_new_reply_can_send_immediately(self):
        self.store.queue_progress("old", "old", ["第一条", "第二条", "第三条"], "napcat")
        waiting = asyncio.Event()
        stop = asyncio.Event()
        sent = []
        original_gap = self.daemon._wait_outbox_gap

        async def gap(outbox_id, delay):
            waiting.set()
            await original_gap(outbox_id, delay)

        async def send(payload):
            sent.append(payload["segments"][0]["data"]["text"])
            if sent[-1] == "新回复":
                stop.set()
            return "sent"

        self.daemon._wait_outbox_gap = gap
        self.daemon.channel.send_message = send
        with patch("momoi.runtime.dispatch.delivery.random.uniform", return_value=60):
            worker = asyncio.create_task(self.daemon._outbox_worker(stop))
            try:
                await asyncio.wait_for(waiting.wait(), timeout=1)
                await self.daemon._receive(self.message)
                self.store.queue_progress("new", "new", ["新回复"], "napcat")
                self.daemon.outbox_changed.set()
                await asyncio.wait_for(worker, timeout=1)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(sent, ["第一条", "新回复"])

    async def test_new_message_during_voice_synthesis_prevents_delivery(self):
        self.store.queue_progress("old", "voice", ["旧语音"], "napcat", voice=True)
        stop = asyncio.Event()

        async def synthesize(_text):
            await self.daemon._receive(self.message)
            self.store.queue_progress("new", "new", ["新回复"], "napcat")
            return AudioOutput(b"audio", "silk")

        async def send(_payload):
            stop.set()
            return "sent"

        self.daemon.bubble_delivery.tts_provider = AsyncMock()
        self.daemon.bubble_delivery.tts_provider.synthesize.side_effect = synthesize
        self.daemon.channel.send_voice = AsyncMock()
        self.daemon.channel.send_message = send
        await asyncio.wait_for(self.daemon._outbox_worker(stop), timeout=1)
        self.daemon.channel.send_voice.assert_not_awaited()
        self.assertEqual([tuple(row) for row in self.store._db.execute(
            "SELECT text, state FROM outbox ORDER BY id"
        )], [("旧语音", "superseded"), ("新回复", "sent")])

    async def test_in_flight_send_finishes_but_remaining_bubbles_are_cancelled(self):
        self.store.queue_progress("old", "old", ["发送中", "排队中"], "napcat")
        stop = asyncio.Event()
        sent = []

        async def send(payload):
            text = payload["segments"][0]["data"]["text"]
            sent.append(text)
            if text == "发送中":
                await self.daemon._receive(self.message)
                self.store.queue_progress("new", "new", ["新回复"], "napcat")
            else:
                stop.set()
            return "sent"

        self.daemon.channel.send_message = send
        await asyncio.wait_for(self.daemon._outbox_worker(stop), timeout=1)
        self.assertEqual(sent, ["发送中", "新回复"])
        self.assertEqual([tuple(row) for row in self.store._db.execute(
            "SELECT text, state FROM outbox ORDER BY id"
        )], [("发送中", "sent"), ("排队中", "superseded"), ("新回复", "sent")])
