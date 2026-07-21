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

from .channel import incoming_segments, render_segments
from .config import NapCatConfig
from .models import IncomingMessage

logger = logging.getLogger(__name__)


class NapCatError(RuntimeError):
    pass


class NotConnected(NapCatError):
    pass


class AmbiguousSend(NapCatError):
    pass


class SendRejected(NapCatError):
    pass


class NapCatClient:
    def __init__(self, config: NapCatConfig) -> None:
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ready = asyncio.Event()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()
        self._inbound_lock = asyncio.Lock()

    async def run(
        self,
        on_message: Callable[[IncomingMessage], Awaitable[None]],
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
                                    self._handle_payload(payload, on_message)
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
        on_message: Callable[[IncomingMessage], Awaitable[None]],
    ) -> None:
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
            await on_message(
                IncomingMessage(
                    event_id=f"qq:{self_id}:{message_id}",
                    message_id=message_id,
                    text=text,
                    occurred_at=occurred_at,
                    received_at=time.time(),
                    segments=segments,
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
        except (ValueError, NapCatError):
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
        except NapCatError:
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
