import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


from momoi.channel import (
    NotConnected,
    SendRejected,
)
from momoi.channel.napcat import (
    NapCatChannel,
    NapCatConfig,
    image_blocks,
    incoming_segments,
    render_segments,
)
from momoi.channel.weixin import WeixinConfig
from momoi.config import (
    AppConfig,
    LLMConfig,
)
from momoi.runtime import (
    MomoiDaemon,
)
from momoi.models import (
    AgentReply,
    IncomingMessage,
    OwnerInputStatus,
    ProviderResponse,
    ToolCall,
)
from momoi.storage import Store
from tests.support import with_context_planner


class MessagingTest(unittest.TestCase):
    def test_napcat_forwards_only_owner_input_status_notices(self) -> None:
        async def run() -> None:
            client = NapCatChannel(
                NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20)
            )
            accepted: list[IncomingMessage | OwnerInputStatus] = []

            async def receive(event: IncomingMessage | OwnerInputStatus) -> None:
                accepted.append(event)

            for event_type, status_text in ((1, "对方正在输入..."), (0, "")):
                await client._handle_payload(
                    {
                        "post_type": "notice",
                        "notice_type": "notify",
                        "sub_type": "input_status",
                        "self_id": 10000,
                        "user_id": 20000,
                        "event_type": event_type,
                        "status_text": status_text,
                    },
                    receive,
                )
            await client._handle_payload(
                {
                    "post_type": "notice",
                    "notice_type": "notify",
                    "sub_type": "input_status",
                    "self_id": 10000,
                    "user_id": 30000,
                    "event_type": 1,
                },
                receive,
            )

            self.assertEqual(
                accepted,
                [OwnerInputStatus("napcat"), OwnerInputStatus("napcat")],
            )

        asyncio.run(run())

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

    def test_renders_napcat_face_names_with_unknown_fallback(self) -> None:
        self.assertEqual(
            render_segments([{"type": "face", "data": {"id": "32"}}]),
            "[QQ face id=32 description=疑问]",
        )
        self.assertEqual(
            render_segments([{"type": "face", "data": {"id": "999999"}}]),
            "[QQ face id=999999]",
        )
        self.assertEqual(
            render_segments(
                [{"type": "face", "data": {"id": "32", "summary": "自定义名称"}}]
            ),
            "[QQ face id=32 description=自定义名称]",
        )

    def test_napcat_resolves_quoted_message_content_and_images(self) -> None:
        async def run() -> None:
            client = NapCatChannel(
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
            client = NapCatChannel(
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
                IncomingMessage(
                    "qq:1:rich",
                    "rich",
                    "附件",
                    1,
                    2,
                    segments,
                    channel="napcat",
                )
            )
            store.close()
            store = Store(path)
            restored = store.pending_events()[0]
            self.assertEqual(restored.segments, segments)
            self.assertEqual(restored.channel, "napcat")
            store.close()

    def test_validates_terminal_response_tool(self) -> None:
        reply, error = MomoiDaemon._parse_response(
            {
                "expects_reply": True,
                "reply_expectation": "主人晚上的安排",
                "mood": {"decision": "unchanged"},
            }
        )
        self.assertIsNone(error)
        self.assertEqual(reply.messages, [])
        self.assertTrue(reply.expects_reply)
        self.assertEqual(reply.reply_expectation, "主人晚上的安排")
        legacy, error = MomoiDaemon._parse_response(
            {
                "messages": ["旧协议消息"],
                "expects_reply": False,
                "reply_expectation": "",
                "mood": {"decision": "unchanged"},
            }
        )
        self.assertIsNone(legacy)
        self.assertEqual(error, "messages_not_allowed_in_respond")
        heartbeat, error = MomoiDaemon._parse_response(
            {
                "expects_reply": False,
                "reply_expectation": "",
                "mood": {"decision": "unchanged"},
                "heartbeat": {
                    "continue_waiting_for_reply": False,
                    "activity": "整理关卡灵感",
                    "result": "记下一个点子",
                    "next_check_minutes": 10,
                    "reason": "有值得保留的想法",
                },
            },
            require_heartbeat=True,
        )
        self.assertIsNone(error)
        self.assertEqual(heartbeat.heartbeat["activity"], "整理关卡灵感")
        invalid_heartbeat, error = MomoiDaemon._parse_response(
            {
                "expects_reply": False,
                "reply_expectation": "",
                "mood": {"decision": "unchanged"},
            },
            require_heartbeat=True,
        )
        self.assertIsNone(invalid_heartbeat)
        self.assertEqual(error, "invalid_heartbeat_state")
        invalid_blank_lines, error = MomoiDaemon._parse_messages(
            {"messages": ["第一条。\n\n第二条。"]}
        )
        self.assertIsNone(invalid_blank_lines)
        self.assertEqual(error, "blank_lines_must_be_separate_messages")
        single_line_break, error = MomoiDaemon._parse_messages(
            {"messages": ["第一行。\n第二行。"]}
        )
        self.assertIsNone(error)
        self.assertEqual(single_line_break, ["第一行。\n第二行。"])
        invalid, error = MomoiDaemon._parse_response(
            {
                "mood": {"decision": "unchanged"},
            }
        )
        self.assertIsNone(invalid)
        self.assertEqual(error, "invalid_expects_reply")
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
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
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

            daemon.channel.send_message = send_message  # type: ignore[method-assign]
            with (
                patch("momoi.runtime.daemon.random.uniform", return_value=3),
                patch("momoi.runtime.daemon.asyncio.sleep", new=sleep),
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

    async def test_respond_can_close_a_turn_without_a_visible_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="test",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)
            case = self

            class Provider:
                async def complete(
                    self,
                    _: object,
                    __: object,
                    tools: list[dict[str, object]],
                    **___: object,
                ) -> ProviderResponse:
                    respond = next(tool for tool in tools if tool["name"] == "respond")
                    case.assertNotIn("messages", respond["input_schema"]["properties"])
                    call = ToolCall(
                        "silent-close",
                        "respond",
                        {
                            "expects_reply": False,
                            "reply_expectation": "",
                            "mood": {"decision": "unchanged"},
                        },
                    )
                    return ProviderResponse([], [call])

            provider = Provider()
            daemon.provider = with_context_planner(provider)  # type: ignore[assignment]
            event = IncomingMessage("qq:silent-close", "silent-close", "[表情]", 1, 1)
            daemon.store.add_event(event)
            await daemon._complete_batch_turn(
                [event], asyncio.Event(), daemon._turn_id(event.event_id)
            )

            self.assertEqual(daemon.store.due_outbox(), [])
            self.assertEqual(
                daemon.store._db.execute(
                    "SELECT role FROM messages ORDER BY id DESC LIMIT 1"
                ).fetchone()[0],
                "user",
            )
            self.assertEqual(daemon.store.pending_events(), [])
            daemon.store.close()

    async def test_empty_respond_can_wait_for_a_send_message_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                    system_prompt="test",
                    recent_raw_tokens=1000,
                    recent_turns=2,
                    memory_results=2,
                    memory_tokens=1000,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )

            class Provider:
                calls = 0

                async def complete(
                    self,
                    _: object,
                    __: object,
                    ___: object,
                    **____: object,
                ) -> ProviderResponse:
                    self.calls += 1
                    if self.calls == 1:
                        return ProviderResponse(
                            [],
                            [
                                ToolCall(
                                    "live-question",
                                    "send_message",
                                    {
                                        "messages": ["老师会选哪一个？"],
                                    },
                                )
                            ],
                        )
                    return ProviderResponse(
                        [],
                        [
                            ToolCall(
                                "close-after-question",
                                "respond",
                                {
                                    "expects_reply": True,
                                    "reply_expectation": "老师的选择",
                                    "mood": {"decision": "unchanged"},
                                },
                            )
                        ],
                    )

            daemon.provider = with_context_planner(Provider())  # type: ignore[assignment]
            event = IncomingMessage(
                "owner-live-question", "owner-live-question", "你觉得选哪个", 1, 1
            )
            daemon.store.add_event(event)
            await daemon._complete_batch_turn(
                [event], asyncio.Event(), daemon._turn_id(event.event_id)
            )

            outbox = daemon.store.due_outbox()
            self.assertEqual([row.text for row in outbox], ["老师会选哪一个？"])
            self.assertEqual(
                daemon.store._db.execute(
                    "SELECT reply_expectation FROM outbox WHERE id=?", (outbox[0].id,)
                ).fetchone()[0],
                "老师的选择",
            )
            daemon.store.close()

    async def test_outbox_does_not_wait_between_different_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
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

            daemon.channel.send_message = send_message  # type: ignore[method-assign]
            with patch("momoi.runtime.daemon.asyncio.sleep", new=unexpected_sleep):
                await daemon._outbox_worker(stop)

            self.assertEqual(sent, ["第一轮", "第二轮"])
            daemon.store.close()

    async def test_pure_image_input_reaches_owner_queue_and_llm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 0.01, 60, 30, 30, 20),
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

            await daemon.channel._handle_payload(  # type: ignore[attr-defined]
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
                            "expects_reply": False,
                            "reply_expectation": "",
                            "messages": ["看到了"],
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
            daemon.provider = with_context_planner(provider)  # type: ignore[assignment]
            daemon.store.add_event(accepted[0])
            turn_id = daemon._turn_id(accepted[0].event_id)
            daemon.store.begin_turn(turn_id, "owner", [accepted[0].event_id])
            await daemon._complete_batch(accepted, turn_id)
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
            second_asset = root / "proud.jpg"
            asset.write_bytes(b"image")
            second_asset.write_bytes(b"image")
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 0.01, 60, 30, 30, 20),
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
            daemon.store.add_emotion("proud-1", second_asset, "得意收尾时使用")

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
                            "expects_reply": False,
                            "reply_expectation": "",
                            "messages": [
                                "太好了",
                                "emotion://happy-1",
                                "这次我可厉害了",
                                "emotion://proud-1",
                            ],
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
            daemon.provider = with_context_planner(provider)  # type: ignore[assignment]
            event = IncomingMessage("qq:1:emotion", "emotion", "好消息", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])
            await daemon._complete_batch([event], turn_id)
            request = json.dumps(provider.messages, ensure_ascii=False)
            self.assertIn("happy-1", request)
            self.assertIn("proud-1", request)
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
            daemon.store.mark_sent(image.id)
            second_text = daemon.store.due_outbox()[0]
            self.assertEqual(second_text.text, "这次我可厉害了")
            daemon.store.mark_sent(second_text.id)
            second_image = daemon.store.due_outbox()[0]
            self.assertEqual(second_image.kind, "image")
            self.assertEqual(second_image.media_path, str(second_asset.resolve()))
            daemon.store.close()

    async def test_unknown_emotion_directive_is_returned_to_llm_for_correction(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 0.01, 60, 30, 30, 20),
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
                            "expects_reply": False,
                            "reply_expectation": "",
                            "messages": [value],
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
            daemon.provider = with_context_planner(provider)  # type: ignore[assignment]
            event = IncomingMessage("qq:1:emotion-fix", "emotion-fix", "回我", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])
            await daemon._complete_batch([event], turn_id)
            self.assertEqual(provider.calls, 2)
            self.assertIn("unknown_emotion_slug", provider.errors[0])
            self.assertEqual(daemon.store.due_outbox()[0].text, "改成文字回复")
            daemon.store.close()

    async def test_napcat_sends_emotion_as_base64_image_segment(self) -> None:
        client = NapCatChannel(
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
        client = NapCatChannel(
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

    async def test_channels_share_context_but_reply_to_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            napcat = NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20)
            weixin = WeixinConfig.from_mapping({}, root)
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=napcat,
                    channels=(napcat, weixin),
                    system_prompt="test",
                    recent_raw_tokens=1000,
                    recent_turns=2,
                    memory_results=2,
                    memory_tokens=1000,
                    database=root / "momoi.sqlite3",
                    log_level="INFO",
                    workspace=root,
                )
            )
            case = self

            class Provider:
                calls = 0

                async def complete(
                    self,
                    _: object,
                    messages: list[dict[str, object]],
                    tools: list[dict[str, object]],
                    **__: object,
                ) -> ProviderResponse:
                    self.calls += 1
                    serialized = json.dumps(messages, ensure_ascii=False)
                    if self.calls == 2:
                        case.assertIn("QQ 上说过的事", serialized)
                        case.assertIn("Channel: weixin", serialized)
                        spec = next(
                            tool for tool in tools if tool["name"] == "send_message"
                        )
                        channel = spec["input_schema"]["properties"][  # type: ignore[index]
                            "channel"
                        ]
                        case.assertEqual(
                            channel["enum"],  # type: ignore[index]
                            ["napcat", "weixin"],
                        )
                        case.assertEqual(
                            channel["default"],
                            "napcat",  # type: ignore[index]
                        )
                    if self.calls in {2, 3}:
                        arguments: dict[str, object] = {
                            "messages": [f"进度 {self.calls - 1}"],
                        }
                        if self.calls == 2:
                            arguments["channel"] = "weixin"
                        return ProviderResponse(
                            [],
                            [
                                ToolCall(
                                    f"progress-{self.calls}",
                                    "send_message",
                                    arguments,
                                )
                            ],
                        )
                    text = "QQ 回复" if self.calls == 1 else "微信回复"
                    return ProviderResponse(
                        [],
                        [
                            ToolCall(
                                f"respond-{self.calls}",
                                "respond",
                                {
                                    "expects_reply": False,
                                    "reply_expectation": "",
                                    "messages": [text],
                                    "mood": {"decision": "unchanged"},
                                },
                            )
                        ],
                    )

            daemon.provider = with_context_planner(Provider())  # type: ignore[assignment]
            for event in (
                IncomingMessage(
                    "napcat:1", "1", "QQ 上说过的事", 1, 1, channel="napcat"
                ),
                IncomingMessage("weixin:2", "2", "接着刚才聊", 2, 2, channel="weixin"),
            ):
                daemon.store.add_event(event)
                turn_id = daemon._turn_id(event.event_id)
                daemon.store.begin_turn(turn_id, "owner", [event.event_id])
                await daemon._complete_batch([event], turn_id)

            rows = daemon.store._db.execute(
                "SELECT text, target_channel FROM outbox ORDER BY id"
            ).fetchall()
            self.assertEqual(
                [(row["text"], row["target_channel"]) for row in rows],
                [
                    ("QQ 回复", "napcat"),
                    ("进度 1", "weixin"),
                    ("进度 2", "napcat"),
                    ("微信回复", "weixin"),
                ],
            )
            qq_update = IncomingMessage(
                "napcat:update", "3", "QQ 补充", 3, 3, channel="napcat"
            )
            weixin_update = IncomingMessage(
                "weixin:update", "4", "微信另聊", 4, 4, channel="weixin"
            )
            daemon.incoming.put_nowait(qq_update)
            daemon.incoming.put_nowait(weixin_update)
            self.assertEqual(daemon._drain_owner_updates([], "napcat"), [qq_update])
            self.assertEqual(daemon._deferred_incoming.popleft(), weixin_update)
            daemon.store.close()

    async def test_disconnected_channel_does_not_block_another_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            napcat = NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20)
            weixin = WeixinConfig.from_mapping({}, root)
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=napcat,
                    channels=(napcat, weixin),
                    system_prompt="test",
                    recent_raw_tokens=1000,
                    recent_turns=2,
                    memory_results=2,
                    memory_tokens=1000,
                    database=root / "momoi.sqlite3",
                    log_level="INFO",
                    workspace=root,
                )
            )
            daemon.store.commit_turn(
                [],
                "",
                AgentReply(["QQ pending"]),
                turn_id="qq",
                target_channel="napcat",
            )
            daemon.store.commit_turn(
                [],
                "",
                AgentReply(["Weixin ready"]),
                turn_id="weixin",
                target_channel="weixin",
            )
            sent: list[str] = []
            stop = asyncio.Event()

            async def offline(_: dict[str, object]) -> str:
                raise NotConnected("offline")

            async def online(payload: dict[str, object]) -> str:
                sent.append(payload["segments"][0]["data"]["text"])  # type: ignore[index]
                stop.set()
                return "sent"

            daemon.channels["napcat"].send_message = offline  # type: ignore[method-assign]
            daemon.channels["weixin"].send_message = online  # type: ignore[method-assign]
            await daemon._outbox_worker(stop)

            self.assertEqual(sent, ["Weixin ready"])
            states = dict(
                daemon.store._db.execute(
                    "SELECT target_channel, state FROM outbox"
                ).fetchall()
            )
            self.assertEqual(states, {"napcat": "pending", "weixin": "sent"})
            daemon.store.close()
