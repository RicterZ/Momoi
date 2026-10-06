from tests.support import install_scripted_replyer
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.models import IncomingMessage
from momoi.runtime import MomoiDaemon
from tests.support import provider_catalog, recall_response
from tests.test_images import incoming


class ImageTurnsTest(unittest.IsolatedAsyncioTestCase):
    async def test_summary_is_private_and_second_turn_can_reread_original(self):
        requests = []
        image_id = ""
        finish = {"reply_wait": {"wait": False}, "mood": {"decision": "unchanged"}}
        recall = recall_response().tool_calls[0]
        actions = [
            (recall.name, recall.arguments),
            (
                "end_turn",
                finish,
            ),  # Missing summary must be corrected, not silently forgotten.
            ("save_image_summary", {"summary": "白色小图，主体细节不明确。"}),
            ("reply", {"intent": "回应图片", "reference": json.dumps(["我看到这张图了。"], ensure_ascii=False)}),
            ("end_turn", finish),
            (recall.name, recall.arguments),
            ("read_image", {}),
            (
                "save_image_summary",
                {"summary": "重新查看：白色小图，没有可辨认的文字。"},
            ),
            ("end_turn", finish),
        ]

        async def completion(request):
            payload = await request.json()
            index = len(requests)
            requests.append(payload)
            name, args = actions[min(index, len(actions) - 1)]
            args = dict(args)
            if name in {"read_image", "save_image_summary"}:
                args["image_id"] = image_id
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": f"call-{index}",
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": json.dumps(args),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", completion)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                providers=provider_catalog(
                    LLMConfig(
                        str(server.make_url("/v1")),
                        "test",
                        "vision-test",
                        1000,
                        0.6,
                        2,
                        0,
                        "openai",
                    )
                ),
                channel=NapCatConfig("ws://127.0.0.1", "123", 1, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_unsummarized_tail_turns=2,
                memory_results=2,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)
            install_scripted_replyer(daemon)
            try:
                async with daemon.services:
                    event = incoming()
                    daemon.store.add_event(event)
                    image_id = event.segments[0]["data"]["_image_id"]
                    await daemon._complete_batch_turn(
                        [event], asyncio.Event(), daemon._turn_id(event.event_id)
                    )
                    second = IncomingMessage(
                        "followup",
                        "followup",
                        "图里有文字吗？",
                        time.time(),
                        time.time(),
                    )
                    daemon.store.add_event(second)
                    await daemon._complete_batch_turn(
                        [second], asyncio.Event(), daemon._turn_id(second.event_id)
                    )
                self.assertEqual(len(requests), len(actions))
                self.assertIn("image_summary_required", json.dumps(requests[2]))
                history = json.dumps(requests[5], ensure_ascii=False)
                self.assertIn("白色小图", history)
                self.assertIn(image_id, history)
                self.assertNotIn("data:image", history)
                self.assertIn("data:image/png;base64,", json.dumps(requests[7]))
                self.assertEqual(
                    [row.text for row in daemon.store.due_outbox()],
                    ["我看到这张图了。"],
                )
                visible = daemon.store._db.execute(
                    "SELECT content FROM messages"
                ).fetchall()
                self.assertNotIn("白色小图", str([tuple(row) for row in visible]))
            finally:
                daemon.store.close()
