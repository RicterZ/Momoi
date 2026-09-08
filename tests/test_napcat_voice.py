import asyncio
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, call, patch

from momoi.channel import IncomingVoice, NotConnected, SendRejected
from momoi.channel.napcat import NapCatChannel, NapCatConfig
from momoi.channel.napcat.channel import VOICE_UNAVAILABLE_TEXT


class NapCatVoiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel = NapCatChannel(
            NapCatConfig("ws://localhost", "20000", 1, 60, 30, 30, 20)
        )
        self.voice = IncomingVoice(message_id="123")

    async def test_waits_for_qq_to_generate_text(self):
        self.channel._request_action = AsyncMock(side_effect=[
            SendRejected("not ready"),
            {"data": {"text": " "}},
            {"data": {"text": " 转写完成 "}},
        ])
        with patch("momoi.channel.napcat.channel.asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await self.channel.convert_voice(self.voice), "转写完成")
        self.assertEqual(sleep.await_args_list, [call(2), call(4)])
        self.assertEqual(self.channel._request_action.await_args_list, [
            call("fetch_ptt_text", {"message_id": "123"})
        ] * 3)

    async def test_rejections_and_invalid_results_have_bounded_retries(self):
        for response in [SendRejected("unavailable"), {}, {"data": None},
                         {"data": {"text": ""}}, {"data": {"text": 42}}]:
            with self.subTest(response=response):
                self.channel._request_action = AsyncMock(
                    side_effect=response if isinstance(response, Exception) else None,
                    return_value=response,
                )
                with patch("momoi.channel.napcat.channel.asyncio.sleep", new_callable=AsyncMock):
                    self.assertEqual(await self.channel.convert_voice(self.voice), VOICE_UNAVAILABLE_TEXT)
                self.assertEqual(self.channel._request_action.await_count, 3)

    async def test_disconnection_does_not_retry(self):
        self.channel._request_action = AsyncMock(side_effect=NotConnected("offline"))
        self.assertEqual(await self.channel.convert_voice(self.voice), VOICE_UNAVAILABLE_TEXT)
        self.channel._request_action.assert_awaited_once()

    async def test_missing_message_id_does_not_call_napcat(self):
        self.channel._request_action = AsyncMock()
        self.assertEqual(await self.channel.convert_voice(IncomingVoice()), VOICE_UNAVAILABLE_TEXT)
        self.channel._request_action.assert_not_awaited()

    async def test_total_timeout_includes_retry_delay(self):
        self.channel.config = replace(self.channel.config, send_timeout_seconds=0.02)
        self.channel._request_action = AsyncMock(side_effect=SendRejected("not ready"))
        self.assertEqual(await self.channel.convert_voice(self.voice), VOICE_UNAVAILABLE_TEXT)
        self.channel._request_action.assert_awaited_once()

    async def test_cancellation_propagates(self):
        self.channel._request_action = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.channel.convert_voice(self.voice)
        self.channel._request_action.assert_awaited_once()
