import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


from momoi.asr import ASRProvider, AudioInput
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
from momoi.channel.napcat.channel import VOICE_UNAVAILABLE_TEXT
from momoi.channel.weixin import WeixinConfig
from momoi.config.models import (
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
from momoi.reply_wait import decode_reply_wait, encode_reply_wait
from momoi.runtime.parsing import (
    parse_activity_decision,
    parse_bubbles,
    parse_response,
)
from momoi.storage import Store
from tests.support import with_owner_recall


class MessagingTest(unittest.TestCase):
    def test_napcat_converts_voice_bubbles_before_emitting_message(self) -> None:
        class Provider(ASRProvider):
            def __init__(self) -> None:
                self.inputs: list[AudioInput] = []

            async def transcribe(self, audio: AudioInput) -> str:
                self.inputs.append(audio)
                return "语音转写结果"

        async def run() -> None:
            provider = Provider()
            client = NapCatChannel(
                NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                provider,
                1024,
            )
            requests: list[tuple[str, dict[str, object]]] = []

            async def request(
                action: str, params: dict[str, object]
            ) -> dict[str, object]:
                requests.append((action, params))
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"base64": base64.b64encode(b"voice").decode()},
                }

            client._request_action = request  # type: ignore[method-assign]
            accepted: list[IncomingMessage] = []

            async def receive(message: IncomingMessage) -> None:
                accepted.append(message)

            payload = {
                "post_type": "message",
                "message_type": "private",
                "self_id": 10000,
                "user_id": 20000,
                "message_id": 1,
                "message": [{"type": "record", "data": {"file": "voice.silk"}}],
            }
            await client._handle_payload(payload, receive)

            self.assertEqual(
                requests,
                [("get_record", {"file": "voice.silk", "out_format": "mp3"})],
            )
            self.assertEqual(provider.inputs, [AudioInput(b"voice", "mp3")])
            self.assertEqual(accepted[0].text, "语音转写结果")
            self.assertEqual(
                accepted[0].segments,
                ({"type": "text", "data": {"text": "语音转写结果"}},),
            )

            disabled = NapCatChannel(
                NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20)
            )
            placeholders: list[IncomingMessage] = []

            async def receive_disabled(message: IncomingMessage) -> None:
                placeholders.append(message)

            await disabled._handle_payload(payload, receive_disabled)
            self.assertEqual(placeholders[0].text, VOICE_UNAVAILABLE_TEXT)
            self.assertEqual(placeholders[0].segments[0]["type"], "text")
            self.assertNotIn("voice.silk", str(placeholders[0].segments))

        asyncio.run(run())

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
        self.assertIn("source=remote", rendered)
        self.assertIn("，卧室的", rendered)
        self.assertIn("天气卡片", rendered)
        self.assertIn("今天有雨", rendered)
        self.assertIn("https://weather.test", rendered)
        self.assertIn("QQ sticker", rendered)
        self.assertIn("动画表情", rendered)
        self.assertIn("戳一戳", rendered)

    def test_materializes_remote_napcat_images_within_limit(self) -> None:
        parsed = NapCatConfig.from_mapping(
            {
                "url": "ws://127.0.0.1",
                "owner_qq": "20000",
                "media_max_bytes": 4,
                "media_download_timeout_seconds": 2,
            }
        )
        self.assertEqual((parsed.media_max_bytes, parsed.media_download_timeout_seconds), (4, 2))

        class Content:
            async def iter_chunked(self, _size: int):
                yield b"image-bytes"

        class Response:
            status = 200
            content_length = len(b"image-bytes")
            headers = {"Content-Type": "image/png"}
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

        class Session:
            def get(self, source: str, *, timeout: object):
                self.source = source
                self.timeout = timeout
                return Response()

        async def run() -> None:
            client = NapCatChannel(
                NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20)
            )
            client._session = Session()  # type: ignore[assignment]
            segments = await client._enrich_segments(
                (
                    {
                        "type": "image",
                        "data": {"url": "https://img.example/image.png"},
                    },
                )
            )
            block = image_blocks(segments)[0]
            self.assertEqual(block["source"]["type"], "base64")
            self.assertEqual(block["source"]["media_type"], "image/png")
            self.assertEqual(
                block["source"]["data"], base64.b64encode(b"image-bytes").decode()
            )
            self.assertIn("source=embedded", render_segments(segments))

            limited = NapCatChannel(
                NapCatConfig(
                    "ws://127.0.0.1",
                    "20000",
                    1,
                    60,
                    30,
                    30,
                    20,
                    media_max_bytes=4,
                )
            )
            limited._session = Session()  # type: ignore[assignment]
            unavailable = await limited._enrich_segments(
                (
                    {
                        "type": "image",
                        "data": {"url": "https://img.example/large.png"},
                    },
                )
            )
            self.assertEqual(image_blocks(unavailable), [])
            self.assertIn("source=unavailable", render_segments(unavailable))

        asyncio.run(run())

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

    def test_napcat_stickers_are_described_but_not_sent_as_vision(self) -> None:
        segments = [
            {
                "type": "mface",
                "data": {
                    "emoji_id": "market-1",
                    "summary": "摇头",
                    "url": "https://img.example/market.gif",
                },
            },
            {
                "type": "image",
                "data": {
                    "sub_type": 1,
                    "summary": "动画表情",
                    "url": "https://img.example/sticker.gif",
                },
            },
            {
                "type": "image",
                "data": {
                    "sub_type": 0,
                    "url": "https://img.example/photo.jpg",
                },
            },
        ]

        self.assertEqual(
            image_blocks(segments),
            [
                {
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": "https://img.example/photo.jpg",
                    },
                }
            ],
        )
        rendered = render_segments(segments)
        self.assertIn("[QQ sticker id=market-1 description=摇头]", rendered)
        self.assertIn("[QQ sticker", rendered)

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
        self.assertIsNone(decode_reply_wait("旧版纯文本期待"))
        encoded = encode_reply_wait("老师的安排", "需要安排晚上活动", 6)
        self.assertEqual(
            decode_reply_wait(encoded),
            {
                "expected_information": "老师的安排",
                "reason": "需要安排晚上活动",
                "delay_minutes": 6,
            },
        )
        reply, error = parse_response(
            {
                "reply_wait": {"wait": False},
                "mood": {"decision": "unchanged"},
            }
        )
        self.assertIsNone(error)
        self.assertEqual(reply.messages, [])
        self.assertFalse(reply.expects_reply)
        self.assertFalse(reply.should_schedule_reply_wait)
        scheduled, error = parse_response(
            {
                "reply_wait": {
                    "wait": True,
                    "delay_minutes": 7,
                    "expected_information": "主人晚上的安排",
                    "reason": "晚上约好一起玩，需要知道主人什么时候有空",
                },
                "mood": {"decision": "unchanged"},
            }
        )
        self.assertIsNone(error)
        self.assertTrue(scheduled.should_schedule_reply_wait)
        self.assertEqual(scheduled.reply_expectation, "主人晚上的安排")
        self.assertEqual(scheduled.reply_wait_delay_minutes, 7)
        self.assertIn("一起玩", scheduled.reply_wait_reason)
        invalid_schedule, error = parse_response(
            {
                "reply_wait": {
                    "wait": True,
                    "delay_minutes": 11,
                    "expected_information": "主人晚上的安排",
                    "reason": "需要按约定跟进",
                },
                "mood": {"decision": "unchanged"},
            }
        )
        self.assertIsNone(invalid_schedule)
        self.assertEqual(error, "invalid_reply_wait_decision")
        with_bubbles, error = parse_response(
            {
                "bubbles": ["旧协议气泡"],
                "reply_wait": {"wait": False},
                "mood": {"decision": "unchanged"},
            }
        )
        self.assertIsNone(with_bubbles)
        self.assertEqual(error, "bubbles_not_allowed_in_end_turn")
        old_shape, error = parse_response(
            {
                "expects_reply": False,
                "reply_expectation": "",
                "mood": {"decision": "unchanged"},
            }
        )
        self.assertIsNone(old_shape)
        self.assertEqual(error, "legacy_reply_wait_fields_not_allowed")
        heartbeat, error = parse_response(
            {
                "reply_wait": {"wait": False},
                "mood": {"decision": "unchanged"},
                "heartbeat": {
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
        invalid_heartbeat, error = parse_response(
            {
                "reply_wait": {"wait": False},
                "mood": {"decision": "unchanged"},
            },
            require_heartbeat=True,
        )
        self.assertIsNone(invalid_heartbeat)
        self.assertEqual(error, "invalid_heartbeat_state")
        empty_bubbles, error = parse_bubbles({"bubbles": []})
        self.assertIsNone(empty_bubbles)
        self.assertEqual(error, "bubbles_must_be_a_non_empty_array")
        invalid_blank_lines, error = parse_bubbles(
            {"bubbles": ["第一条。\n\n第二条。"]}
        )
        self.assertIsNone(invalid_blank_lines)
        self.assertEqual(error, "blank_lines_must_be_separate_bubbles")
        single_line_break, error = parse_bubbles(
            {"bubbles": ["第一行。\n第二行。"]}
        )
        self.assertIsNone(error)
        self.assertEqual(single_line_break, ["第一行。\n第二行。"])
        invalid, error = parse_response(
            {
                "mood": {"decision": "unchanged"},
            }
        )
        self.assertIsNone(invalid)
        self.assertEqual(error, "invalid_reply_wait_decision")
        rich, error = parse_bubbles(
            {
                "bubbles": [
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
        invalid_rich, error = parse_bubbles(
            {
                "bubbles": [
                    {
                        "segments": [
                            {"type": "text", "data": {"text": "第一段\n\n第二段"}}
                        ]
                    }
                ]
            }
        )
        self.assertIsNone(invalid_rich)
        self.assertEqual(error, "blank_lines_must_be_separate_bubbles")
        mixed, error = parse_bubbles(
            {
                "bubbles": [
                    {
                        "segments": [
                            {
                                "type": "text",
                                "data": {"text": "找到啦老师！"},
                            },
                            {
                                "type": "file",
                                "data": {"file": "/tmp/concept.md"},
                            },
                        ]
                    }
                ]
            }
        )
        self.assertIsNone(error)
        self.assertEqual(len(mixed), 2)
        self.assertEqual(
            mixed[0]["segments"],
            [{"type": "text", "data": {"text": "找到啦老师！"}}],
        )
        self.assertEqual(
            mixed[1]["segments"],
            [{"type": "file", "data": {"file": "/tmp/concept.md"}}],
        )
        captioned_image, error = parse_bubbles(
            {
                "bubbles": [
                    {
                        "segments": [
                            {"type": "text", "data": {"text": "看看这张"}},
                            {"type": "image", "data": {"file": "/tmp/a.png"}},
                        ]
                    }
                ]
            }
        )
        self.assertIsNone(error)
        self.assertEqual(len(captioned_image), 1)
        self.assertEqual(len(captioned_image[0]["segments"]), 2)
        file_then_text, error = parse_bubbles(
            {
                "bubbles": [
                    {
                        "segments": [
                            {"type": "file", "data": {"file": "/tmp/a.md"}},
                            {"type": "text", "data": {"text": "附件在上面"}},
                        ]
                    }
                ]
            }
        )
        self.assertIsNone(error)
        self.assertEqual(
            [item["segments"][0]["type"] for item in file_then_text],
            ["file", "text"],
        )

    def test_owner_activity_decision_is_explicit_and_gated(self) -> None:
        self.assertEqual(
            parse_activity_decision({"decision": "unchanged"}),
            (None, None),
        )
        updated, error = parse_activity_decision(
            {
                "decision": "updated",
                "text": "和老师聊清双人操控能力限制，停下今晚的合作准备",
                "result": "双人合作推迟到 agent 能力升级以后",
            }
        )
        self.assertIsNone(error)
        self.assertEqual(updated["text"], "和老师聊清双人操控能力限制，停下今晚的合作准备")

        reply, error = parse_response(
            {
                "reply_wait": {"wait": False},
                "mood": {"decision": "unchanged"},
                "activity": {
                    "decision": "updated",
                    "text": "和老师聊清双人操控能力限制，停下今晚的合作准备",
                    "result": "双人合作推迟到 agent 能力升级以后",
                },
            },
            allow_activity_update=True,
        )
        self.assertIsNone(error)
        self.assertEqual(reply.activity_update, updated)

        missing, error = parse_response(
            {
                "reply_wait": {"wait": False},
                "mood": {"decision": "unchanged"},
            },
            allow_activity_update=True,
        )
        self.assertIsNone(missing)
        self.assertEqual(error, "invalid_activity_decision")

        disallowed, error = parse_response(
            {
                "reply_wait": {"wait": False},
                "mood": {"decision": "unchanged"},
                "activity": {"decision": "unchanged"},
            }
        )
        self.assertIsNone(disallowed)
        self.assertEqual(error, "activity_update_not_allowed")

    def test_commit_turn_uses_owner_occurred_at_for_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            first = IncomingMessage(
                "napcat:1:10", "10", "嗯 好 约定好了", 1_755_349_611.0, 1_755_349_640.0
            )
            second = IncomingMessage(
                "napcat:1:11", "11", "那就这么定", 1_755_349_620.0, 1_755_349_641.0
            )
            before = time.time()
            store.commit_turn(
                [first, second],
                "嗯 好 约定好了",
                AgentReply(["嗯！约定好了"]),
                turn_id="turn-owner-time",
            )
            after = time.time()
            rows = store._db.execute(
                """SELECT role, created_at FROM messages
                   WHERE turn_id='turn-owner-time' ORDER BY id"""
            ).fetchall()
            self.assertEqual([row["role"] for row in rows], ["user", "assistant"])
            self.assertEqual(rows[0]["created_at"], first.occurred_at)
            self.assertGreaterEqual(rows[1]["created_at"], before)
            self.assertLessEqual(rows[1]["created_at"], after)
            self.assertGreater(rows[1]["created_at"], rows[0]["created_at"])
            store.close()


class MessagingAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_consecutive_similar_send_bubbles_returns_tool_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 0.01, 60, 30, 30, 20
                    ),
                    system_prompt="You are Momoi.",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )

            class Provider:
                calls = 0
                warning = ""

                async def complete(
                    self,
                    _: object,
                    messages: list[dict[str, object]],
                    *__: object,
                    **___: object,
                ) -> ProviderResponse:
                    self.calls += 1
                    if self.calls == 1:
                        call = ToolCall(
                            "first-message",
                            "send_bubbles",
                            {
                                "bubbles": [
                                    "嗝得这么响亮，这顿吃得超满意嘛",
                                    "吃饱了就好，下午接着瘫着养精神",
                                ]
                            },
                        )
                    elif self.calls == 2:
                        call = ToolCall(
                            "similar-message",
                            "send_bubbles",
                            {
                                "bubbles": [
                                    "嗝得这么响，看来这顿很满意嘛",
                                    "吃饱了就好，下午接着舒服瘫着",
                                ]
                            },
                        )
                    else:
                        self.warning = json.dumps(messages[-1], ensure_ascii=False)
                        call = ToolCall(
                            "close-after-warning",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
                                "activity": {"decision": "unchanged"},
                            },
                        )
                    return ProviderResponse([], [call])

            provider = Provider()
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]
            event = IncomingMessage("qq:similar", "similar", "吃完饭啦", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])
            await daemon._complete_batch([event], turn_id)

            self.assertEqual(provider.calls, 3)
            self.assertIn("similar_bubbles_already_sent", provider.warning)
            self.assertIn("already sent successfully", provider.warning)
            self.assertIn('"is_error": true', provider.warning)
            self.assertEqual(
                [
                    str(row["text"])
                    for row in daemon.store._db.execute(
                        "SELECT text FROM outbox ORDER BY id"
                    ).fetchall()
                ],
                [
                    "嗝得这么响亮，这顿吃得超满意嘛",
                    "吃饱了就好，下午接着瘫着养精神",
                ],
            )
            daemon.store.close()

    async def test_owner_receives_leading_proactive_bubbles_as_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 0.01, 60, 30, 30, 20
                    ),
                    system_prompt="You are Momoi.",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            now = time.time()
            with daemon.store._db:
                daemon.store._db.execute(
                    """INSERT INTO turns
                       (id, kind, source_ids_json, state, started_at, updated_at)
                       VALUES ('heartbeat:proactive', 'autonomous', '[]',
                               'completed', ?, ?)""",
                    (now - 20, now - 10),
                )
                daemon.store._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at, source_event_ids_json,
                        delivery_state)
                       VALUES ('heartbeat:proactive', 'assistant', ?, ?, '[]',
                               'delivered')""",
                    ("刚才提醒你窗户还开着", now - 10),
                )

            class Provider:
                messages: list[dict[str, object]] = []

                async def complete(
                    self,
                    _system: object,
                    messages: list[dict[str, object]],
                    *__: object,
                    **___: object,
                ) -> ProviderResponse:
                    self.messages = messages
                    return ProviderResponse(
                        [],
                        [
                            ToolCall(
                                "finish",
                                "end_turn",
                                {
                                    "reply_wait": {"wait": False},
                                    "mood": {"decision": "unchanged"},
                                    "activity": {"decision": "unchanged"},
                                },
                            )
                        ],
                    )

            provider = Provider()
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]
            event = IncomingMessage("qq:after-proactive", "1", "看到了", now, now)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])
            await daemon._complete_batch([event], turn_id)

            wire = json.dumps(provider.messages, ensure_ascii=False)
            self.assertIn("<delivered_proactive_bubbles>", wire)
            self.assertIn("刚才提醒你窗户还开着", wire)
            self.assertIn(
                "already delivered before the retained owner transcript", wire
            )
            self.assertFalse(
                any(
                    message.get("role") == "assistant"
                    and "刚才提醒你窗户还开着" in json.dumps(
                        message, ensure_ascii=False
                    )
                    for message in provider.messages
                )
            )
            daemon.store.close()

    async def test_outbox_waits_only_between_messages_in_the_same_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="test",
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_raw_tail_turns=2,
                memory_results=2,
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
                patch(
                    "momoi.runtime.dispatch.delivery.random.uniform",
                    return_value=3,
                ),
                patch("momoi.runtime.dispatch.delivery.asyncio.sleep", new=sleep),
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

    async def test_end_turn_can_close_a_turn_without_a_visible_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="test",
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_raw_tail_turns=2,
                memory_results=2,
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
                    end_turn = next(tool for tool in tools if tool["name"] == "end_turn")
                    case.assertNotIn("bubbles", end_turn["input_schema"]["properties"])
                    call = ToolCall(
                        "silent-close",
                        "end_turn",
                        {
                            "reply_wait": {"wait": False},
                            "mood": {"decision": "unchanged"},
                            "activity": {"decision": "unchanged"},
                        },
                    )
                    return ProviderResponse([], [call])

            provider = Provider()
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]
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

    async def test_empty_end_turn_can_wait_for_a_send_bubbles_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
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
                                    "send_bubbles",
                                    {
                                        "bubbles": ["老师会选哪一个？"],
                                    },
                                )
                            ],
                        )
                    return ProviderResponse(
                        [],
                        [
                            ToolCall(
                                "close-after-question",
                                "end_turn",
                                {
                                    "reply_wait": {
                                        "wait": True,
                                        "delay_minutes": 4,
                                        "expected_information": "老师的选择",
                                        "reason": "需要按老师的选择继续",
                                    },
                                    "mood": {"decision": "unchanged"},
                                    "activity": {"decision": "unchanged"},
                                },
                            )
                        ],
                    )

            daemon.provider = with_owner_recall(Provider())  # type: ignore[assignment]
            event = IncomingMessage(
                "owner-live-question", "owner-live-question", "你觉得选哪个", 1, 1
            )
            daemon.store.add_event(event)
            await daemon._complete_batch_turn(
                [event], asyncio.Event(), daemon._turn_id(event.event_id)
            )

            outbox = daemon.store.due_outbox()
            self.assertEqual([row.text for row in outbox], ["老师会选哪一个？"])
            stored_wait = json.loads(
                daemon.store._db.execute(
                    "SELECT reply_expectation FROM outbox WHERE id=?", (outbox[0].id,)
                ).fetchone()[0]
            )
            self.assertEqual(stored_wait["expected_information"], "老师的选择")
            self.assertEqual(stored_wait["delay_minutes"], 4)
            daemon.store.close()

    async def test_outbox_does_not_wait_between_different_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="test",
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_raw_tail_turns=2,
                memory_results=2,
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
            with patch(
                "momoi.runtime.dispatch.delivery.asyncio.sleep",
                new=unexpected_sleep,
            ):
                await daemon._outbox_worker(stop)

            self.assertEqual(sent, ["第一轮", "第二轮"])
            daemon.store.close()

    async def test_pure_image_input_reaches_owner_queue_and_llm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 0.01, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_raw_tail_turns=2,
                memory_results=2,
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
            self.assertIn("source=remote", accepted[0].text)

            class Provider:
                def __init__(self) -> None:
                    self.system: object = None
                    self.messages: list[dict[str, object]] = []

                async def complete(
                    self,
                    system: object,
                    messages: list[dict[str, object]],
                    *__: object,
                    **___: object,
                ) -> ProviderResponse:
                    self.system = system
                    self.messages = messages
                    call = ToolCall(
                        "image-response",
                        "end_turn",
                        {
                            "reply_wait": {"wait": False},
                            "mood": {"decision": "unchanged"},
                            "activity": {"decision": "unchanged"},
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
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]
            daemon.store.add_event(accepted[0])
            turn_id = daemon._turn_id(accepted[0].event_id)
            daemon.store.begin_turn(turn_id, "owner", [accepted[0].event_id])
            await daemon._complete_batch(accepted, turn_id)
            owner_message = next(
                message
                for message in provider.messages
                if any(
                    isinstance(block, dict) and block.get("type") == "image"
                    for block in (
                        message["content"]
                        if isinstance(message.get("content"), list)
                        else []
                    )
                )
            )
            blocks = owner_message["content"]
            images = [
                index
                for index, block in enumerate(blocks)
                if block.get("type") == "image"  # type: ignore[union-attr]
            ]
            self.assertEqual(len(images), 1)
            # The attachment follows the text block of the message it arrived
            # with, rather than being appended after the whole batch.
            self.assertEqual(blocks[images[0] - 1]["type"], "text")  # type: ignore[index]
            self.assertLess(images[0], len(blocks) - 1)
            self.assertEqual(
                blocks[images[0]]["source"]["url"],  # type: ignore[index]
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
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_raw_tail_turns=2,
                memory_results=2,
                database=root / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)
            daemon.store.add_emotion("happy-1", asset, "真心高兴或庆祝时使用")
            daemon.store.add_emotion("proud-1", second_asset, "得意收尾时使用")
            case = self

            class Provider:
                def __init__(self) -> None:
                    self.system: object = None
                    self.messages: list[dict[str, object]] = []
                    self.calls = 0

                async def complete(
                    self,
                    system: object,
                    messages: list[dict[str, object]],
                    *__: object,
                    **___: object,
                ) -> ProviderResponse:
                    self.system = system
                    self.messages = messages
                    self.calls += 1
                    if self.calls > 1:
                        case.assertIn(
                            "committed", json.dumps(messages[-1], ensure_ascii=False)
                        )
                        call = ToolCall(
                            "emotion-close",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
                                "activity": {"decision": "unchanged"},
                            },
                        )
                        return ProviderResponse([], [call])
                    call = ToolCall(
                        "emotion-response",
                        "send_bubbles",
                        {
                            "bubbles": [
                                "太好了",
                                "emotion://happy-1",
                                "这次我可厉害了",
                                "emotion://proud-1",
                            ],
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
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]
            event = IncomingMessage("qq:1:emotion", "emotion", "好消息", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])
            await daemon._complete_batch([event], turn_id)
            request = json.dumps(provider.system, ensure_ascii=False)
            self.assertIn("happy-1", request)
            self.assertIn("proud-1", request)
            self.assertIn("真心高兴或庆祝时使用", request)
            self.assertNotIn(str(asset), request)
            self.assertEqual(
                daemon.delivery_policy.validate_emotions(["emotion://missing"]),
                "unknown_emotion_slug",
            )
            self.assertEqual(
                daemon.delivery_policy.validate_emotions(
                    ["你好呀\nemotion://happy-1"]
                ),
                "emotion_directive_must_be_a_standalone_bubble",
            )
            self.assertEqual(
                daemon.delivery_policy.validate_emotions(
                    ["emotion://happy-1 真的假的"]
                ),
                "emotion_directive_must_be_a_standalone_bubble",
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
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_raw_tail_turns=2,
                memory_results=2,
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
                    if self.calls <= 2:
                        value = "emotion://missing" if self.calls == 1 else "改成文字回复"
                        tool_name = "send_bubbles"
                        arguments = {"bubbles": [value]}
                    else:
                        tool_name = "end_turn"
                        arguments = {
                            "reply_wait": {"wait": False},
                            "mood": {"decision": "unchanged"},
                            "activity": {"decision": "unchanged"},
                        }
                    call = ToolCall(
                        f"emotion-{self.calls}",
                        tool_name,
                        arguments,
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
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]
            event = IncomingMessage("qq:1:emotion-fix", "emotion-fix", "回我", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])
            await daemon._complete_batch([event], turn_id)
            self.assertEqual(provider.calls, 3)
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
            unnamed = Path(directory) / "report.pdf"
            unnamed.write_bytes(b"%PDF")
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
                            "type": "file",
                            "data": {"file": str(unnamed)},
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
        self.assertEqual(segments[3]["data"]["name"], "notes.txt")
        encoded_unnamed = segments[4]["data"]["file"].removeprefix("base64://")
        self.assertEqual(base64.b64decode(encoded_unnamed), b"%PDF")
        self.assertEqual(segments[4]["data"]["name"], "report.pdf")
        self.assertEqual(segments[5]["data"]["file"], "https://cdn.example/a.mp4")
        self.assertEqual(segments[7], {"type": "face", "data": {"id": "178"}})
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
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
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
                    if self.calls == 3:
                        case.assertIn("QQ 上说过的事", serialized)
                        case.assertNotIn("Channel: weixin", serialized)
                        case.assertNotIn("[weixin]", serialized)
                        case.assertNotIn("[napcat]", serialized)
                        case.assertNotIn("channel=weixin", serialized)
                        case.assertNotIn("channel=napcat", serialized)
                        spec = next(
                            tool for tool in tools if tool["name"] == "send_bubbles"
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
                            "weixin",  # type: ignore[index]
                        )
                    if self.calls in {1, 3, 4, 5}:
                        text = {
                            1: "QQ 回复",
                            3: "进度 1",
                            4: "进度 2",
                            5: "微信回复",
                        }[self.calls]
                        arguments: dict[str, object] = {
                            "bubbles": [text],
                        }
                        if self.calls == 3:
                            arguments["channel"] = "napcat"
                        return ProviderResponse(
                            [],
                            [
                                ToolCall(
                                    f"progress-{self.calls}",
                                    "send_bubbles",
                                    arguments,
                                )
                            ],
                        )
                    return ProviderResponse(
                        [],
                        [
                            ToolCall(
                                f"end_turn-{self.calls}",
                                "end_turn",
                                {
                                    "reply_wait": {"wait": False},
                                    "mood": {"decision": "unchanged"},
                                    "activity": {"decision": "unchanged"},
                                },
                            )
                        ],
                    )

            daemon.provider = with_owner_recall(Provider())  # type: ignore[assignment]
            now = time.time()
            for event in (
                IncomingMessage(
                    "napcat:1",
                    "1",
                    "QQ 上说过的事",
                    now,
                    now,
                    channel="napcat",
                ),
                IncomingMessage(
                    "weixin:2",
                    "2",
                    "接着刚才聊",
                    now + 2,
                    now + 2,
                    channel="weixin",
                ),
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
                    ("进度 1", "napcat"),
                    ("进度 2", "weixin"),
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
            self.assertEqual(daemon.owner_updates.drain([], "napcat"), [qq_update])
            self.assertEqual(daemon._deferred_incoming.popleft(), weixin_update)

            older_qq = IncomingMessage(
                "napcat:older", "5", "较早的 QQ 补充", 5, 5, channel="napcat"
            )
            newer_qq = IncomingMessage(
                "napcat:newer", "6", "较新的 QQ 补充", 6, 6, channel="napcat"
            )
            daemon._deferred_incoming.append(older_qq)
            daemon.incoming.put_nowait(newer_qq)
            self.assertEqual(
                daemon.owner_updates.drain([], "napcat"), [older_qq, newer_qq]
            )
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
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
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
