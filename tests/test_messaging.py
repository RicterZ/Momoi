import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


from momoi.channel import image_blocks, incoming_segments, render_segments
from momoi.config import (
    AppConfig,
    LLMConfig,
    NapCatConfig,
)
from momoi.daemon import (
    MomoiDaemon,
)
from momoi.models import (
    AgentReply,
    IncomingMessage,
    ProviderResponse,
    ToolCall,
)
from momoi.napcat import NapCatClient, SendRejected
from momoi.store import Store


class MessagingTest(unittest.TestCase):
    def test_renders_mixed_napcat_segments_without_losing_cards(self) -> None:
        payload = {
            "message": [
                {"type": "text", "data": {"text": "先开灯"}},
                {"type": "image", "data": {"url": "https://img.example/a.jpg"}},
                {"type": "text", "data": {"text": "，卧室的"}},
                {
                    "type": "json",
                    "data": {
                        "data": (
                            '{"meta":{"news":{"title":"天气卡片",'
                            '"desc":"今天有雨","jumpUrl":"https://weather.test",'
                            '"tag":"天气"}}}'
                        )
                    },
                },
                {
                    "type": "image",
                    "data": {
                        "url": "https://img.example/sticker.jpg",
                        "sub_type": 1,
                        "summary": "[动画表情]",
                    },
                },
                {
                    "type": "face",
                    "data": {
                        "id": "181",
                        "raw": {"faceText": "[戳一戳]"},
                    },
                },
            ]
        }
        rendered = render_segments(incoming_segments(payload))
        self.assertIn("先开灯", rendered)
        self.assertIn("https://img.example/a.jpg", rendered)
        self.assertIn("，卧室的", rendered)
        self.assertIn("天气卡片", rendered)
        self.assertIn("今天有雨", rendered)
        self.assertIn("https://weather.test", rendered)
        self.assertIn("QQ sticker", rendered)
        self.assertIn("动画表情", rendered)
        self.assertIn("戳一戳", rendered)

    def test_napcat_resolves_quoted_message_content_and_images(self) -> None:
        async def run() -> None:
            client = NapCatClient(
                NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20)
            )
            accepted: list[IncomingMessage] = []
            case = self

            class Socket:
                closed = False

                async def send_json(self, payload: dict[str, object]) -> None:
                    case.assertEqual(payload["action"], "get_msg")
                    case.assertEqual(payload["params"], {"message_id": 77})
                    asyncio.get_running_loop().call_soon(
                        client._resolve_response,
                        {
                            "echo": payload["echo"],
                            "status": "ok",
                            "retcode": 0,
                            "data": {
                                "sender": {
                                    "user_id": 20000,
                                    "nickname": "老师",
                                },
                                "message": [
                                    {
                                        "type": "text",
                                        "data": {"text": "看这张图"},
                                    },
                                    {
                                        "type": "image",
                                        "data": {
                                            "url": "https://img.example/quoted.jpg"
                                        },
                                    },
                                ],
                            },
                        },
                    )

            client._ws = Socket()  # type: ignore[assignment]
            client._ready.set()

            async def receive(message: IncomingMessage) -> None:
                accepted.append(message)

            await client._handle_payload(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": 10000,
                    "user_id": 20000,
                    "message_id": 88,
                    "message": [
                        {"type": "reply", "data": {"id": "77"}},
                        {
                            "type": "text",
                            "data": {"text": "这张挺可爱的"},
                        },
                    ],
                },
                receive,
            )
            self.assertEqual(len(accepted), 1)
            rendered = render_segments(accepted[0].segments)
            self.assertIn("老师(20000)", rendered)
            self.assertIn("看这张图", rendered)
            self.assertIn("这张挺可爱的", rendered)
            self.assertEqual(
                image_blocks(accepted[0].segments)[0]["source"]["url"],
                "https://img.example/quoted.jpg",
            )

        asyncio.run(run())

    def test_napcat_resolves_forward_nodes_and_images(self) -> None:
        async def run() -> None:
            client = NapCatClient(
                NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20)
            )
            client._ready.set()

            async def request(
                action: str, params: dict[str, object]
            ) -> dict[str, object]:
                self.assertEqual(action, "get_forward_msg")
                self.assertEqual(params, {"message_id": "forward-1"})
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": {
                        "messages": [
                            {
                                "sender": {"user_id": 1, "nickname": "Alice"},
                                "content": [
                                    {"type": "text", "data": {"text": "节点正文"}},
                                    {
                                        "type": "image",
                                        "data": {
                                            "url": "https://img.example/forward.jpg"
                                        },
                                    },
                                ],
                            }
                        ]
                    },
                }

            client._request_action = request  # type: ignore[method-assign]
            segments = await client._enrich_segments(
                ({"type": "forward", "data": {"id": "forward-1"}},)
            )
            rendered = render_segments(segments)
            self.assertIn("Alice", rendered)
            self.assertIn("节点正文", rendered)
            self.assertEqual(
                image_blocks(segments)[0]["source"]["url"],
                "https://img.example/forward.jpg",
            )

        asyncio.run(run())

    def test_incoming_segments_survive_event_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "momoi.sqlite3"
            segments = (
                {"type": "reply", "data": {"id": "8"}},
                {"type": "file", "data": {"name": "notes.txt", "file": "https://x/f"}},
            )
            store = Store(path)
            store.add_event(
                IncomingMessage("qq:1:rich", "rich", "附件", 1, 2, segments)
            )
            store.close()
            store = Store(path)
            restored = store.pending_events()[0]
            self.assertEqual(restored.segments, segments)
            store.close()

    def test_validates_terminal_response_tool(self) -> None:
        reply, error = MomoiDaemon._parse_response(
            {
                "messages": ["嘿嘿，没忘吧~", "晚上在忙什么呢？"],
                "continuity": {
                    "topic": "晚上的安排",
                    "open_loops": ["等待对方说晚上的安排"],
                    "pending_commitments": [],
                    "short_term_facts": [],
                },
                "mood": {"action": "keep"},
            }
        )
        self.assertIsNone(error)
        self.assertEqual(reply.messages, ["嘿嘿，没忘吧~", "晚上在忙什么呢？"])
        self.assertEqual(reply.continuity["topic"], "晚上的安排")
        invalid, error = MomoiDaemon._parse_response(
            {"messages": ["第一条。\n\n第二条。"]}
        )
        self.assertIsNone(invalid)
        self.assertEqual(error, "blank_lines_must_be_separate_messages")
        rich, error = MomoiDaemon._parse_messages(
            {
                "messages": [
                    {
                        "segments": [
                            {"type": "reply", "data": {"id": "9"}},
                            {"type": "text", "data": {"text": "收到"}},
                        ]
                    }
                ]
            }
        )
        self.assertIsNone(error)
        self.assertEqual(rich[0]["action"], "message")
        invalid_rich, error = MomoiDaemon._parse_messages(
            {
                "messages": [
                    {
                        "segments": [
                            {"type": "text", "data": {"text": "第一段\n\n第二段"}}
                        ]
                    }
                ]
            }
        )
        self.assertIsNone(invalid_rich)
        self.assertEqual(error, "blank_lines_must_be_separate_messages")


class MessagingAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_outbox_waits_only_between_messages_in_the_same_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                napcat=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="test",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)
            daemon.store.commit_turn(
                [],
                "",
                AgentReply(["第一条", "第二条", "第三条"]),
                turn_id="turn-one",
            )
            stop = asyncio.Event()
            timeline: list[tuple[str, object]] = []

            async def send_message(payload: dict[str, object]) -> None:
                text = payload["segments"][0]["data"]["text"]  # type: ignore[index]
                timeline.append(("send", text))
                if text == "第三条":
                    stop.set()

            async def sleep(delay: float) -> None:
                timeline.append(("sleep", delay))

            daemon.napcat.send_message = send_message  # type: ignore[method-assign]
            with (
                patch("momoi.daemon.random.uniform", return_value=3),
                patch("momoi.daemon.asyncio.sleep", new=sleep),
            ):
                await daemon._outbox_worker(stop)

            self.assertEqual(
                timeline,
                [
                    ("send", "第一条"),
                    ("sleep", 3),
                    ("send", "第二条"),
                    ("sleep", 3),
                    ("send", "第三条"),
                ],
            )
            daemon.store.close()

    async def test_outbox_does_not_wait_between_different_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                napcat=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="test",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)
            daemon.store.commit_turn([], "", AgentReply(["第一轮"]), turn_id="turn-one")
            daemon.store.commit_turn([], "", AgentReply(["第二轮"]), turn_id="turn-two")
            stop = asyncio.Event()
            sent: list[str] = []

            async def send_message(payload: dict[str, object]) -> None:
                text = payload["segments"][0]["data"]["text"]  # type: ignore[index]
                sent.append(text)
                if len(sent) == 2:
                    stop.set()

            async def unexpected_sleep(_: float) -> None:
                self.fail("different turns must not inherit an outbox delay")

            daemon.napcat.send_message = send_message  # type: ignore[method-assign]
            with patch("momoi.daemon.asyncio.sleep", new=unexpected_sleep):
                await daemon._outbox_worker(stop)

            self.assertEqual(sent, ["第一轮", "第二轮"])
            daemon.store.close()

    async def test_pure_image_input_reaches_owner_queue_and_llm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                napcat=NapCatConfig("ws://127.0.0.1", "20000", 0.01, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)
            accepted: list[IncomingMessage] = []

            async def receive(message: IncomingMessage) -> None:
                accepted.append(message)

            await daemon.napcat._handle_payload(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": 10000,
                    "user_id": 20000,
                    "message_id": 88,
                    "time": 1,
                    "message": [
                        {
                            "type": "image",
                            "data": {"url": "https://img.example/owner.jpg"},
                        }
                    ],
                },
                receive,
            )
            self.assertEqual(len(accepted), 1)
            self.assertIn("owner.jpg", accepted[0].text)

            class Provider:
                def __init__(self) -> None:
                    self.messages: list[dict[str, object]] = []

                async def complete(
                    self,
                    _: object,
                    messages: list[dict[str, object]],
                    *__: object,
                    **___: object,
                ) -> ProviderResponse:
                    self.messages = messages
                    call = ToolCall(
                        "image-response",
                        "respond",
                        {
                            "messages": ["看到了"],
                            "continuity": {
                                "topic": "",
                                "open_loops": [],
                                "pending_commitments": [],
                                "short_term_facts": [],
                            },
                            "mood": {"action": "keep"},
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
            daemon.store.add_event(accepted[0])
            await daemon._complete_batch(
                accepted, daemon._turn_id(accepted[0].event_id)
            )
            blocks = provider.messages[-1]["content"]
            self.assertEqual(blocks[1]["type"], "image")  # type: ignore[index]
            self.assertEqual(
                blocks[1]["source"]["url"],  # type: ignore[index]
                "https://img.example/owner.jpg",
            )
            daemon.store.close()

    async def test_emotion_catalog_validates_directive_and_queues_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "happy.jpg"
            asset.write_bytes(b"image")
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                napcat=NapCatConfig("ws://127.0.0.1", "20000", 0.01, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
                database=root / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)
            daemon.store.add_emotion("happy-1", asset, "真心高兴或庆祝时使用")

            class Provider:
                def __init__(self) -> None:
                    self.messages: list[dict[str, object]] = []

                async def complete(
                    self,
                    _: object,
                    messages: list[dict[str, object]],
                    *__: object,
                    **___: object,
                ) -> ProviderResponse:
                    self.messages = messages
                    call = ToolCall(
                        "emotion-response",
                        "respond",
                        {
                            "messages": [
                                "太好了",
                                {
                                    "segments": [
                                        {
                                            "type": "text",
                                            "data": {"text": "emotion://happy-1"},
                                        }
                                    ]
                                },
                            ],
                            "continuity": {
                                "topic": "",
                                "open_loops": [],
                                "pending_commitments": [],
                                "short_term_facts": [],
                            },
                            "mood": {"action": "keep"},
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
            event = IncomingMessage("qq:1:emotion", "emotion", "好消息", 1, 1)
            daemon.store.add_event(event)
            await daemon._complete_batch([event], daemon._turn_id(event.event_id))
            request = json.dumps(provider.messages, ensure_ascii=False)
            self.assertIn("happy-1", request)
            self.assertIn("真心高兴或庆祝时使用", request)
            self.assertNotIn(str(asset), request)
            self.assertEqual(
                daemon._validate_emotion_messages(["emotion://missing"]),
                "unknown_emotion_slug",
            )
            first = daemon.store.due_outbox()[0]
            self.assertEqual(first.kind, "text")
            daemon.store.mark_sent(first.id)
            image = daemon.store.due_outbox()[0]
            self.assertEqual(image.kind, "image")
            self.assertEqual(image.media_path, str(asset.resolve()))
            daemon.store.close()

    async def test_unknown_emotion_directive_is_returned_to_llm_for_correction(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                napcat=NapCatConfig("ws://127.0.0.1", "20000", 0.01, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)

            class Provider:
                def __init__(self) -> None:
                    self.calls = 0
                    self.errors: list[str] = []

                async def complete(
                    self,
                    _: object,
                    messages: list[dict[str, object]],
                    *__: object,
                    **___: object,
                ) -> ProviderResponse:
                    self.calls += 1
                    if self.calls > 1:
                        self.errors.append(json.dumps(messages[-1], ensure_ascii=False))
                    value = "emotion://missing" if self.calls == 1 else "改成文字回复"
                    call = ToolCall(
                        f"emotion-{self.calls}",
                        "respond",
                        {
                            "messages": [value],
                            "continuity": {
                                "topic": "",
                                "open_loops": [],
                                "pending_commitments": [],
                                "short_term_facts": [],
                            },
                            "mood": {"action": "keep"},
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
            event = IncomingMessage("qq:1:emotion-fix", "emotion-fix", "回我", 1, 1)
            daemon.store.add_event(event)
            await daemon._complete_batch([event], daemon._turn_id(event.event_id))
            self.assertEqual(provider.calls, 2)
            self.assertIn("unknown_emotion_slug", provider.errors[0])
            self.assertEqual(daemon.store.due_outbox()[0].text, "改成文字回复")
            daemon.store.close()

    async def test_napcat_sends_emotion_as_base64_image_segment(self) -> None:
        client = NapCatClient(
            NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20)
        )
        payloads: list[dict[str, object]] = []

        class Socket:
            closed = False

            async def send_json(self, payload: dict[str, object]) -> None:
                payloads.append(payload)
                echo = str(payload["echo"])
                client._pending[echo].set_result(
                    {"status": "ok", "retcode": 0, "data": {"message_id": 9}}
                )

        client._ws = Socket()  # type: ignore[assignment]
        client._ready.set()
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "asset.png"
            asset.write_bytes(b"image-bytes")
            image = {
                "action": "message",
                "segments": [{"type": "image", "data": {"file": str(asset)}}],
            }
            self.assertEqual(await client.send_message(image), "9")
            with self.assertRaisesRegex(SendRejected, "cannot be read"):
                image["segments"][0]["data"]["file"] = str(
                    Path(directory) / "missing.png"
                )
                await client.send_message(image)
        segment = payloads[0]["params"]["message"][0]  # type: ignore[index]
        self.assertEqual(segment["type"], "image")
        encoded = segment["data"]["file"].removeprefix("base64://")
        self.assertEqual(base64.b64decode(encoded), b"image-bytes")

    async def test_napcat_sends_rich_segments_and_forward_messages(self) -> None:
        client = NapCatClient(
            NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20)
        )
        payloads: list[dict[str, object]] = []

        class Socket:
            closed = False

            async def send_json(self, payload: dict[str, object]) -> None:
                payloads.append(payload)
                echo = str(payload["echo"])
                client._pending[echo].set_result(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"message_id": len(payloads)},
                    }
                )

        client._ws = Socket()  # type: ignore[assignment]
        client._ready.set()
        with tempfile.TemporaryDirectory() as directory:
            file_path = Path(directory) / "notes.txt"
            file_path.write_bytes(b"hello")
            await client.send_message(
                {
                    "action": "message",
                    "segments": [
                        {"type": "reply", "data": {"id": "7"}},
                        {"type": "text", "data": {"text": "附件"}},
                        {
                            "type": "image",
                            "data": {
                                "file": "https://cdn.example/sticker.gif",
                                "sub_type": 1,
                                "summary": "[动画表情]",
                            },
                        },
                        {
                            "type": "file",
                            "data": {"file": str(file_path), "name": "notes.txt"},
                        },
                        {
                            "type": "video",
                            "data": {"file": "https://cdn.example/a.mp4"},
                        },
                        {"type": "json", "data": {"data": '{"app":"card"}'}},
                        {"type": "face", "data": {"id": "178"}},
                    ],
                }
            )
            await client.send_message(
                {
                    "action": "forward",
                    "nodes": [
                        {
                            "type": "node",
                            "data": {
                                "user_id": "20000",
                                "nickname": "Momoi",
                                "content": [{"type": "text", "data": {"text": "节点"}}],
                            },
                        }
                    ],
                }
            )

        normal = payloads[0]
        self.assertEqual(normal["action"], "send_private_msg")
        segments = normal["params"]["message"]  # type: ignore[index]
        self.assertEqual(segments[2]["type"], "image")
        self.assertEqual(segments[2]["data"]["sub_type"], 1)
        encoded = segments[3]["data"]["file"].removeprefix("base64://")
        self.assertEqual(base64.b64decode(encoded), b"hello")
        self.assertEqual(segments[4]["data"]["file"], "https://cdn.example/a.mp4")
        self.assertEqual(segments[6], {"type": "face", "data": {"id": "178"}})
        self.assertEqual(payloads[1]["action"], "send_private_forward_msg")
        self.assertEqual(payloads[1]["params"]["messages"][0]["type"], "node")  # type: ignore[index]
