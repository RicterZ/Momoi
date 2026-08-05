import asyncio
import base64
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import aiohttp

from .. import (
    AmbiguousSend,
    ChannelError,
    NotConnected,
    SendRejected,
)
from ...models import IncomingMessage, OwnerInputStatus
from .face_names import QQ_FACE_NAMES

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NapCatConfig:
    plugin: ClassVar[str] = "napcat"
    url: str
    owner_qq: str
    quiet_seconds: float
    max_batch_seconds: float
    heartbeat_seconds: float
    reconnect_max_seconds: float
    send_timeout_seconds: float

    @classmethod
    def from_mapping(cls, value: object) -> "NapCatConfig":
        if not isinstance(value, dict):
            raise ValueError("channel settings must be a table/object")
        owner_qq = str(value.get("owner_qq") or "")
        if not owner_qq.isdigit():
            raise ValueError("channel.settings.owner_qq must contain digits only")

        def positive(name: str, default: float) -> float:
            number = float(value.get(name, default))
            if number <= 0:
                raise ValueError(f"channel.settings.{name} must be positive")
            return number

        url = str(value.get("url") or "")
        if not url:
            raise ValueError("channel.settings.url is required")
        return cls(
            url=url,
            owner_qq=owner_qq,
            quiet_seconds=positive("quiet_seconds", 1),
            max_batch_seconds=positive("max_batch_seconds", 60),
            heartbeat_seconds=positive("heartbeat_seconds", 30),
            reconnect_max_seconds=positive("reconnect_max_seconds", 30),
            send_timeout_seconds=positive("send_timeout_seconds", 20),
        )


class NapCatChannel:
    name = "napcat"
    prompt_context = "Authenticated private QQ conversation through NapCat with the single owner."

    def __init__(self, config: NapCatConfig) -> None:
        self.config = config
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
                        logger.info("NapCat connected")

                        def finish_inbound(task: asyncio.Task[None]) -> None:
                            inbound_tasks.discard(task)
                            if not task.cancelled() and task.exception() is not None:
                                logger.error(
                                    "NapCat inbound message failed: %s",
                                    type(task.exception()).__name__,
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
                    logger.warning("NapCat disconnected: %s", type(error).__name__)
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
            logger.warning("NapCat sent invalid JSON")
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
            segments = await self._enrich_segments(incoming_segments(payload))
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
        return tuple(enriched)

    async def _enrich_reply(self, segment: dict[str, Any]) -> None:
        message_id = segment["data"].get("id")
        if not isinstance(message_id, (str, int)) or not self._ready.is_set():
            return
        try:
            response = await self._request_action(
                "get_msg", {"message_id": int(message_id)}
            )
        except (ValueError, ChannelError):
            logger.debug("Could not resolve quoted QQ message id=%s", message_id)
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
            logger.debug("Could not resolve forwarded QQ message id=%s", message_id)
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

    async def send_message(self, payload: dict[str, Any]) -> str:
        if payload.get("action") == "forward":
            nodes = []
            for node in payload.get("nodes") or []:
                item = {"type": "node", "data": dict(node.get("data") or {})}
                item["data"]["content"] = await self._prepare_segments(
                    item["data"].get("content") or []
                )
                nodes.append(item)
            return await self._send_action("send_private_forward_msg", {"messages": nodes})
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
            item = {"type": segment.get("type"), "data": dict(segment.get("data") or {})}
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
                if not source.startswith(("base64://", "http://", "https://")):
                    try:
                        content = await asyncio.to_thread(Path(source).expanduser().read_bytes)
                    except OSError as error:
                        raise SendRejected(
                            f"media asset cannot be read: {type(error).__name__}"
                        ) from error
                    item["data"]["file"] = (
                        "base64://" + base64.b64encode(content).decode("ascii")
                    )
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


def load_config(value: object, _workspace: Path) -> NapCatConfig:
    return NapCatConfig.from_mapping(value)


def create_channel(config: object) -> NapCatChannel:
    if not isinstance(config, NapCatConfig):
        raise ValueError("napcat requires NapCatConfig")
    return NapCatChannel(config)


def incoming_segments(payload: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    message = payload.get("message")
    if isinstance(message, str):
        return ({"type": "text", "data": {"text": message}},) if message else ()
    if isinstance(message, list):
        segments = []
        for item in message:
            if not (
                isinstance(item, dict)
                and isinstance(item.get("type"), str)
                and isinstance(item.get("data") or {}, dict)
            ):
                continue
            data = dict(item.get("data") or {})
            if "sub_type" in item and "sub_type" not in data:
                data["sub_type"] = item["sub_type"]
            segments.append({"type": str(item["type"]), "data": data})
        return tuple(segments)
    raw = payload.get("raw_message")
    return ({"type": "text", "data": {"text": str(raw)}},) if raw else ()


def render_segments(
    segments: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> str:
    parts: list[str] = []
    for segment in segments:
        kind = str(segment.get("type") or "unknown")
        data = segment.get("data")
        data = data if isinstance(data, dict) else {}
        if kind == "text":
            value = str(data.get("text") or "")
            if value:
                parts.append(value)
        elif kind == "image":
            label = "sticker" if str(data.get("sub_type", "0")) == "1" else "image"
            parts.append(_describe_media(label, data))
        elif kind in {"file", "video", "record"}:
            parts.append(_describe_media(kind, data))
        elif kind == "reply":
            parts.append(_describe_reply(data))
        elif kind in {"json", "xml"}:
            parts.append(_describe_card(kind, data))
        elif kind == "forward":
            parts.append(_describe_forward(data))
        elif kind in {"face", "mface"}:
            parts.append(_describe_face(kind, data))
        elif kind == "at":
            parts.append(f"[QQ mention qq={data.get('qq', 'unknown')}]")
        elif kind == "location":
            parts.append(
                "[QQ location: "
                f"title={data.get('title', '')} content={data.get('content', '')} "
                f"lat={data.get('lat', '')} lon={data.get('lon', '')}]"
            )
        elif kind == "share":
            parts.append(
                f"[QQ shared link: title={data.get('title', '')} "
                f"url={data.get('url', '')} content={data.get('content', '')}]"
            )
        else:
            parts.append(f"[QQ {kind} segment: {_compact(data)}]")
    return "\n".join(part for part in parts if part).strip()


def image_blocks(
    segments: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for segment in segments:
        kind = segment.get("type")
        data = segment.get("data")
        if kind == "reply" and isinstance(data, dict):
            quoted = data.get("_quoted")
            if isinstance(quoted, dict) and isinstance(quoted.get("segments"), list):
                blocks.extend(image_blocks(quoted["segments"]))
            continue
        if kind == "forward" and isinstance(data, dict):
            nodes = data.get("_forward")
            if isinstance(nodes, list):
                for node in nodes:
                    if isinstance(node, dict) and isinstance(node.get("segments"), list):
                        blocks.extend(image_blocks(node["segments"]))
            continue
        if kind not in {"image", "mface"} or not isinstance(data, dict):
            continue
        source = data.get("url") or data.get("file")
        if not isinstance(source, str):
            continue
        if source.startswith(("http://", "https://")):
            blocks.append({"type": "image", "source": {"type": "url", "url": source}})
        elif source.startswith("base64://"):
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": str(data.get("media_type") or "image/jpeg"),
                        "data": source.removeprefix("base64://"),
                    },
                }
            )
    return blocks


def _describe_media(kind: str, data: dict[str, Any]) -> str:
    source = str(data.get("url") or data.get("file") or "unknown")
    if len(source) > 500:
        source = source[:500] + "...[truncated]"
    label = data.get("summary") or data.get("name")
    suffix = f" description={label}" if label else ""
    return f"[QQ {kind}:{suffix} source={source}]"


def _describe_reply(data: dict[str, Any]) -> str:
    message_id = data.get("id", "unknown")
    quoted = data.get("_quoted")
    if not isinstance(quoted, dict):
        return f"[QQ reply to message_id={message_id}]"
    sender_name = quoted.get("sender_name") or quoted.get("sender_id") or "unknown"
    sender_id = quoted.get("sender_id") or "unknown"
    segments = quoted.get("segments")
    content = render_segments(segments) if isinstance(segments, list) else ""
    if not content:
        content = str(quoted.get("raw_message") or "").strip()
    header = f"[QQ quoted message_id={message_id} from {sender_name}({sender_id})]"
    return f"{header}\n{content}" if content else header


def _describe_forward(data: dict[str, Any]) -> str:
    message_id = data.get("id", "unknown")
    nodes = data.get("_forward")
    if not isinstance(nodes, list):
        return f"[QQ forwarded message id={message_id}]"
    rendered = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        sender = node.get("sender_name") or node.get("sender_id") or "unknown"
        content = render_segments(node.get("segments") or [])
        if content:
            rendered.append(f"{sender}: {content}")
    header = f"[QQ forwarded message id={message_id}]"
    return f"{header}\n" + "\n".join(rendered) if rendered else header


def _describe_card(kind: str, data: dict[str, Any]) -> str:
    raw = data.get("data", data)
    if kind == "json" and isinstance(raw, str):
        try:
            card = json.loads(raw)
        except json.JSONDecodeError:
            card = None
        if isinstance(card, dict):
            meta = card.get("meta")
            meta = meta if isinstance(meta, dict) else {}
            detail = next((value for value in meta.values() if isinstance(value, dict)), {})
            fields = (
                ("title", detail.get("title")),
                ("description", detail.get("desc") or detail.get("summary")),
                ("source", detail.get("tag")),
                ("url", detail.get("jumpUrl") or detail.get("url")),
            )
            summary = "; ".join(
                f"{name}={value}" for name, value in fields if str(value or "").strip()
            )
            if not summary and str(card.get("prompt") or "").strip():
                summary = f"prompt={card['prompt']}"
            if summary:
                return f"[QQ json card: {summary}]"
    return f"[QQ {kind} card: {_compact(raw)}]"


def _describe_face(kind: str, data: dict[str, Any]) -> str:
    raw = data.get("raw")
    raw = raw if isinstance(raw, dict) else {}
    identifier = data.get("id") or data.get("emoji_id") or "unknown"
    label = data.get("summary") or data.get("text") or raw.get("faceText") or raw.get("vaspokeName")
    if not label and kind == "face":
        label = QQ_FACE_NAMES.get(str(identifier))
    description = f" description={label}" if label else ""
    return f"[QQ {'sticker' if kind == 'mface' else 'face'} id={identifier}{description}]"


def _compact(value: object, limit: int = 4000) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            pass
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"
