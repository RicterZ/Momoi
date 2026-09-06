from ...integrations.contracts.tts import AudioOutput
from .. import VOICE_MESSAGE_PREFIX
import asyncio
import base64
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp

from .. import (
    AmbiguousSend,
    ChannelError,
    IncomingVoice,
    NotConnected,
    SendRejected,
)
from ...integrations.contracts.asr import ASRProvider, AudioInput
from ...observability.events import log_event
from ...models import IncomingMessage, OwnerInputStatus
from .config import NapCatConfig
from .parsing import (
    image_blocks,
    incoming_segments,
    is_visual_image,
    media_display_name,
    render_segments,
)

logger = logging.getLogger(__name__)
VOICE_UNAVAILABLE_TEXT = "[QQ 语音消息暂时无法转写]"


class NapCatChannel:
    name = "napcat"

    def __init__(
        self,
        config: NapCatConfig,
        asr_provider: ASRProvider | None = None,
        asr_max_audio_bytes: int = 3 * 1024 * 1024,
    ) -> None:
        self.config = config
        self.asr_provider = asr_provider
        self.asr_max_audio_bytes = asr_max_audio_bytes
        self.quiet_seconds = config.quiet_seconds
        self.max_batch_seconds = config.max_batch_seconds
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ready = asyncio.Event()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()
        self._inbound_lock = asyncio.Lock()

    async def run(
        self,
        on_event: Callable[[IncomingMessage | OwnerInputStatus], Awaitable[None]],
        stop: asyncio.Event,
    ) -> None:
        timeout = aiohttp.ClientTimeout(total=None, connect=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self._session = session
            delay = 1.0
            while not stop.is_set():
                inbound_tasks: set[asyncio.Task[None]] = set()
                try:
                    async with session.ws_connect(
                        self.config.url, heartbeat=self.config.heartbeat_seconds
                    ) as ws:
                        self._ws = ws
                        self._ready.set()
                        delay = 1.0
                        log_event(
                            logger,
                            logging.INFO,
                            "channel_connected",
                            channel="napcat",
                        )

                        def finish_inbound(task: asyncio.Task[None]) -> None:
                            inbound_tasks.discard(task)
                            if not task.cancelled() and task.exception() is not None:
                                log_event(
                                    logger,
                                    logging.ERROR,
                                    "channel_inbound_failure",
                                    channel="napcat",
                                    error_type=type(task.exception()).__name__,
                                )

                        async for frame in ws:
                            if frame.type == aiohttp.WSMsgType.TEXT:
                                payload = self._decode_frame(frame.data)
                                if payload is None:
                                    continue
                                if self._resolve_response(payload):
                                    continue
                                task = asyncio.create_task(
                                    self._handle_payload(payload, on_event)
                                )
                                inbound_tasks.add(task)
                                task.add_done_callback(finish_inbound)
                            elif frame.type in {
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            }:
                                break
                        self._ready.clear()
                        self._fail_pending()
                        if inbound_tasks:
                            await asyncio.gather(*inbound_tasks, return_exceptions=True)
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                    log_event(
                        logger,
                        logging.WARNING,
                        "channel_disconnected",
                        channel="napcat",
                        error_type=type(error).__name__,
                        reconnect_delay_seconds=delay,
                    )
                finally:
                    self._ready.clear()
                    self._ws = None
                    self._fail_pending()
                    for task in inbound_tasks:
                        task.cancel()
                    if inbound_tasks:
                        await asyncio.gather(*inbound_tasks, return_exceptions=True)
                if not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                    delay = min(delay * 2, self.config.reconnect_max_seconds)

    @staticmethod
    def _decode_frame(raw: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log_event(
                logger,
                logging.WARNING,
                "channel_frame_invalid",
                channel="napcat",
                reason="invalid_json",
            )
            return None
        return payload if isinstance(payload, dict) else None

    def _resolve_response(self, payload: dict[str, Any]) -> bool:
        echo = payload.get("echo")
        if echo is None:
            return False
        future = self._pending.pop(str(echo), None)
        if future and not future.done():
            future.set_result(payload)
        return True

    async def _handle_payload(
        self,
        payload: dict[str, Any],
        on_event: Callable[[IncomingMessage | OwnerInputStatus], Awaitable[None]],
    ) -> None:
        if (
            payload.get("post_type") == "notice"
            and payload.get("notice_type") == "notify"
            and payload.get("sub_type") == "input_status"
            and str(payload.get("user_id")) == self.config.owner_qq
        ):
            await on_event(OwnerInputStatus(self.name))
            return
        if (
            payload.get("post_type") != "message"
            or payload.get("message_type") != "private"
            or str(payload.get("user_id")) != self.config.owner_qq
        ):
            return
        async with self._inbound_lock:
            segments = await self._convert_voice_segments(incoming_segments(payload))
            segments = await self._enrich_segments(segments)
            text = render_segments(segments)
            message_id = str(payload.get("message_id", ""))
            if not text or not message_id:
                return
            self_id = str(payload.get("self_id", "unknown"))
            occurred_at = float(payload.get("time") or time.time())
            await on_event(
                IncomingMessage(
                    event_id=f"napcat:{self_id}:{message_id}",
                    message_id=message_id,
                    text=text,
                    occurred_at=occurred_at,
                    received_at=time.time(),
                    segments=segments,
                    channel=self.name,
                )
            )

    async def _convert_voice_segments(
        self, segments: tuple[dict[str, Any], ...]
    ) -> tuple[dict[str, Any], ...]:
        converted: list[dict[str, Any]] = []
        for segment in segments:
            if segment.get("type") != "record":
                converted.append(segment)
                continue
            data = segment.get("data")
            data = data if isinstance(data, dict) else {}
            text = await self.convert_voice(
                IncomingVoice(source=str(data.get("file") or ""))
            )
            converted.append(
                {
                    "type": "text",
                    "data": {"text": VOICE_MESSAGE_PREFIX + (text or VOICE_UNAVAILABLE_TEXT)},
                }
            )
        return tuple(converted)

    async def convert_voice(self, voice: IncomingVoice) -> str | None:
        provider = self.asr_provider
        if provider is None:
            return VOICE_UNAVAILABLE_TEXT
        if not voice.source.strip():
            return VOICE_UNAVAILABLE_TEXT
        try:
            response = await self._request_action(
                "get_record",
                {"file": voice.source.strip(), "out_format": "mp3"},
            )
            data = response.get("data")
            encoded = data.get("base64") if isinstance(data, dict) else None
            if not isinstance(encoded, str) or not encoded:
                raise ValueError("get_record returned no audio")
            content = base64.b64decode(encoded, validate=True)
            if not content:
                raise ValueError("get_record returned empty audio")
            if len(content) > self.asr_max_audio_bytes:
                raise ValueError("audio exceeds ASR size limit")
            text = (await provider.transcribe(AudioInput(content, "mp3"))).strip()
            if not text:
                raise ValueError("ASR returned empty text")
            return text
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_event(
                logger,
                logging.WARNING,
                "channel_voice_conversion_failure",
                channel=self.name,
                error_type=type(error).__name__,
            )
            return VOICE_UNAVAILABLE_TEXT

    async def _enrich_segments(
        self, segments: tuple[dict[str, Any], ...]
    ) -> tuple[dict[str, Any], ...]:
        enriched = [
            {"type": segment["type"], "data": dict(segment.get("data") or {})}
            for segment in segments
        ]
        for segment in enriched:
            if segment["type"] == "reply":
                await self._enrich_reply(segment)
            elif segment["type"] == "forward":
                await self._enrich_forward(segment)
        await self._materialize_images(enriched)
        return tuple(enriched)

    async def _materialize_images(self, segments: list[dict[str, Any]]) -> None:
        if self._session is None:
            return
        for segment in segments:
            data = segment.get("data")
            if not isinstance(data, dict):
                continue
            if segment.get("type") == "reply":
                quoted = data.get("_quoted")
                if isinstance(quoted, dict) and isinstance(
                    quoted.get("segments"), list
                ):
                    await self._materialize_images(quoted["segments"])
            elif segment.get("type") == "forward":
                nodes = data.get("_forward")
                if isinstance(nodes, list):
                    for node in nodes:
                        if isinstance(node, dict) and isinstance(
                            node.get("segments"), list
                        ):
                            await self._materialize_images(node["segments"])
            if not is_visual_image(segment):
                continue
            source = data.get("url") or data.get("file")
            if not isinstance(source, str) or not source.startswith(
                ("http://", "https://")
            ):
                continue
            materialized = await self._download_image(source)
            if materialized is None:
                for key in ("url", "file"):
                    value = data.get(key)
                    if isinstance(value, str) and value.startswith(
                        ("http://", "https://")
                    ):
                        data.pop(key)
                data["_media_unavailable"] = True
                continue
            encoded, media_type = materialized
            data["url"] = "base64://" + encoded
            data["media_type"] = media_type

    async def _download_image(self, source: str) -> tuple[str, str] | None:
        session = self._session
        if session is None:
            return None
        try:
            timeout = aiohttp.ClientTimeout(
                total=self.config.media_download_timeout_seconds
            )
            async with session.get(source, timeout=timeout) as response:
                if response.status >= 400:
                    raise ValueError(f"HTTP {response.status}")
                declared = response.content_length
                if declared is not None and declared > self.config.media_max_bytes:
                    raise ValueError("content too large")
                content = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    content.extend(chunk)
                    if len(content) > self.config.media_max_bytes:
                        raise ValueError("content too large")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                if not content_type.startswith("image/"):
                    content_type = "image/jpeg"
                return base64.b64encode(content).decode("ascii"), content_type
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            log_event(
                logger,
                logging.DEBUG,
                "channel_media_failure",
                channel="napcat",
                media_type="image",
                error_type=type(error).__name__,
            )
            return None

    async def _enrich_reply(self, segment: dict[str, Any]) -> None:
        message_id = segment["data"].get("id")
        if not isinstance(message_id, (str, int)) or not self._ready.is_set():
            return
        try:
            response = await self._request_action(
                "get_msg", {"message_id": int(message_id)}
            )
        except (ValueError, ChannelError):
            log_event(
                logger,
                logging.DEBUG,
                "channel_reference_failure",
                channel="napcat",
                message_id=message_id,
                reference_kind="quote",
            )
            return
        data = response.get("data")
        if not isinstance(data, dict):
            return
        sender = data.get("sender")
        sender = sender if isinstance(sender, dict) else {}
        segment["data"]["_quoted"] = {
            "sender_id": str(sender.get("user_id") or "unknown"),
            "sender_name": str(
                sender.get("card") or sender.get("nickname") or "unknown"
            ),
            "raw_message": str(data.get("raw_message") or ""),
            "segments": list(incoming_segments(data)),
        }

    async def _enrich_forward(self, segment: dict[str, Any]) -> None:
        message_id = segment["data"].get("id")
        if not isinstance(message_id, (str, int)) or not self._ready.is_set():
            return
        try:
            response = await self._request_action(
                "get_forward_msg", {"message_id": str(message_id)}
            )
        except ChannelError:
            log_event(
                logger,
                logging.DEBUG,
                "channel_reference_failure",
                channel="napcat",
                message_id=message_id,
                reference_kind="forward",
            )
            return
        data = response.get("data")
        nodes = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(nodes, list):
            return
        rendered = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            sender = node.get("sender")
            sender = sender if isinstance(sender, dict) else {}
            content = node.get("content", node.get("message"))
            node_segments = incoming_segments({"message": content})
            if not node_segments:
                continue
            rendered.append(
                {
                    "sender_id": str(sender.get("user_id") or "unknown"),
                    "sender_name": str(
                        sender.get("card") or sender.get("nickname") or "unknown"
                    ),
                    "segments": list(node_segments),
                }
            )
        if rendered:
            segment["data"]["_forward"] = rendered

    async def send_voice(self, audio: AudioOutput) -> str:
        """Send in-memory audio as a standalone OneBot record."""
        if not isinstance(audio, AudioOutput) or not isinstance(audio.data, bytes) or not audio.data:
            raise SendRejected("voice requires nonempty audio bytes")
        if audio.format not in {"mp3", "wav", "opus", "silk"}:
            raise SendRejected("unsupported voice audio format")
        return await self._send_segments([{
            "type": "record",
            "data": {"file": "base64://" + base64.b64encode(audio.data).decode("ascii")},
        }])

    async def send_message(self, payload: dict[str, Any]) -> str:
        if payload.get("action") == "forward":
            nodes = []
            for node in payload.get("nodes") or []:
                item = {"type": "node", "data": dict(node.get("data") or {})}
                item["data"]["content"] = await self._prepare_segments(
                    item["data"].get("content") or []
                )
                nodes.append(item)
            return await self._send_action(
                "send_private_forward_msg", {"messages": nodes}
            )
        return await self._send_segments(payload.get("segments") or [])

    async def _send_segments(self, segments: list[dict[str, Any]]) -> str:
        return await self._send_action(
            "send_private_msg", {"message": await self._prepare_segments(segments)}
        )

    async def _prepare_segments(
        self, segments: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for segment in segments:
            item = {
                "type": segment.get("type"),
                "data": dict(segment.get("data") or {}),
            }
            if item["type"] in {"json", "xml"} and isinstance(
                item["data"].get("data"), dict
            ):
                item["data"]["data"] = json.dumps(
                    item["data"]["data"], ensure_ascii=False, separators=(",", ":")
                )
            source = item["data"].get("file")
            if item["type"] in {"image", "file", "video", "record"} and isinstance(
                source, str
            ):
                # base64:// has no filename; NapCat falls back to a UUID without
                # extension unless `name` is set. Prefer an explicit name.
                if item["type"] == "file":
                    name = item["data"].get("name")
                    if not (isinstance(name, str) and name.strip()):
                        derived = media_display_name(source)
                        if derived:
                            item["data"]["name"] = derived
                if not source.startswith(("base64://", "http://", "https://")):
                    try:
                        content = await asyncio.to_thread(
                            Path(source).expanduser().read_bytes
                        )
                    except OSError as error:
                        raise SendRejected(
                            f"media asset cannot be read: {type(error).__name__}"
                        ) from error
                    item["data"]["file"] = "base64://" + base64.b64encode(
                        content
                    ).decode("ascii")
            prepared.append(item)
        return prepared

    async def _send_action(self, action: str, params: dict[str, Any]) -> str:
        response = await self._request_action(
            action,
            {"user_id": int(self.config.owner_qq), **params},
        )
        data = response.get("data")
        data = data if isinstance(data, dict) else {}
        return str(data.get("message_id", ""))

    async def _request_action(
        self, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            await asyncio.wait_for(
                self._ready.wait(), timeout=self.config.send_timeout_seconds
            )
        except TimeoutError as error:
            raise NotConnected("NapCat is not connected") from error
        async with self._send_lock:
            ws = self._ws
            if ws is None or ws.closed:
                raise NotConnected("NapCat is not connected")
            echo = f"momoi:{uuid.uuid4().hex}"
            future = asyncio.get_running_loop().create_future()
            self._pending[echo] = future
            payload = {
                "action": action,
                "params": params,
                "echo": echo,
            }
            try:
                await ws.send_json(payload)
                response = await asyncio.wait_for(
                    future, timeout=self.config.send_timeout_seconds
                )
            except (aiohttp.ClientError, asyncio.TimeoutError, AmbiguousSend) as error:
                self._pending.pop(echo, None)
                raise AmbiguousSend("NapCat send result is unknown") from error
            finally:
                self._pending.pop(echo, None)
            if response.get("status") != "ok" or response.get("retcode") != 0:
                raise SendRejected(
                    f"NapCat rejected {action} with retcode {response.get('retcode')}"
                )
            return response

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(AmbiguousSend("NapCat connection closed"))
        self._pending.clear()

    def workflow_variables(self) -> dict[str, str]:
        return {
            "channel_url": self.config.url,
            "owner_id": self.config.owner_qq,
            "napcat_url": self.config.url,
            "owner_qq": self.config.owner_qq,
        }

    def content_blocks(
        self, segments: tuple[dict[str, Any], ...]
    ) -> list[dict[str, Any]]:
        return image_blocks(segments)
