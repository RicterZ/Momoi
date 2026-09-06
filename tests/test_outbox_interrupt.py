from tests.support import provider_catalog
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.models import AgentReply, IncomingMessage, OwnerInputStatus
from momoi.runtime import MomoiDaemon
from momoi.integrations.contracts.tts import AudioOutput


class OutboxInterruptTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.daemon = MomoiDaemon(AppConfig(
            providers=provider_catalog(LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0)),
            channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
            system_prompt="test",
            transcript_turns_min=4,
            transcript_turns_max=4,
            episode_raw_tail_turns=2,
            memory_results=2,
            database=Path(directory.name) / "momoi.sqlite3",
            log_level="INFO",
        ))
        self.store = self.daemon.store
        self.addCleanup(self.store.close)
        self.message = IncomingMessage("new-message", "1", "等一下", 1, 1)

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
