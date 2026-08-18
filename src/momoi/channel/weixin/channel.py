import asyncio
import base64
import hashlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp

from .. import AmbiguousSend, IncomingVoice, NotConnected, SendRejected
from ...logging_context import log_event
from .api import WeixinAPI, WeixinHTTPError
from .config import WeixinConfig, WeixinState
from .media import (
    CDN_BASE_URL,
    download_item,
    encrypt,
    media_type,
    padded_size,
    read_source,
    safe_filename,
)
from ...models import IncomingMessage, OwnerInputStatus


logger = logging.getLogger(__name__)
MESSAGE_TEXT = 1
MESSAGE_IMAGE = 2
MESSAGE_VOICE = 3
MESSAGE_FILE = 4
MESSAGE_VIDEO = 5


class WeixinChannel:
    name = "weixin"
    prompt_context = "Authenticated private Weixin conversation through Tencent iLink with the single owner."

    def __init__(self, config: WeixinConfig) -> None:
        self.config = config
        self.quiet_seconds = config.quiet_seconds
        self.max_batch_seconds = config.max_batch_seconds
        self.state = WeixinState.load(config.state_path)
        self._session: aiohttp.ClientSession | None = None
        self._ready = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._paused_until = 0.0

    async def run(
        self,
        on_event: Callable[[IncomingMessage | OwnerInputStatus], Awaitable[None]],
        stop: asyncio.Event,
    ) -> None:
        if self.state is None:
            raise RuntimeError("Weixin is not logged in; run `momoi channel login`")
        timeout = aiohttp.ClientTimeout(total=None, connect=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self._session = session
            api = WeixinAPI(
                session,
                self.state.base_url,
                self.state.token,
                self.config.send_timeout_seconds,
            )
            await self._notify(api, "notifystart")
            self._ready.set()
            log_event(
                logger,
                logging.INFO,
                "channel_connected",
                channel="weixin",
                account=self.state.account_id,
            )
            failures = 0
            next_timeout = 35.0
            try:
                while not stop.is_set():
                    if self._paused_until > time.monotonic():
                        await _wait(stop, self._paused_until - time.monotonic())
                        continue
                    try:
                        response = await api.post(
                            "ilink/bot/getupdates",
                            {
                                "get_updates_buf": self.state.get_updates_buf,
                                "base_info": api.base_info(),
                            },
                            timeout=next_timeout,
                        )
                    except asyncio.TimeoutError:
                        failures = 0
                        continue
                    except asyncio.CancelledError:
                        raise
                    except (aiohttp.ClientError, RuntimeError) as error:
                        failures += 1
                        log_event(
                            logger,
                            logging.WARNING,
                            "channel_poll_failure",
                            channel="weixin",
                            attempt=failures,
                            error_type=type(error).__name__,
                        )
                        await _wait(
                            stop,
                            self.config.reconnect_max_seconds if failures >= 3 else 2,
                        )
                        if failures >= 3:
                            failures = 0
                        continue

                    suggested = response.get("longpolling_timeout_ms")
                    if isinstance(suggested, (int, float)) and suggested > 0:
                        next_timeout = float(suggested) / 1000
                    result = int(response.get("errcode") or response.get("ret") or 0)
                    if result == -14:
                        self._paused_until = time.monotonic() + 3600
                        log_event(
                            logger,
                            logging.ERROR,
                            "channel_session_stale",
                            channel="weixin",
                            pause_seconds=3600,
                        )
                        continue
                    if result:
                        failures += 1
                        log_event(
                            logger,
                            logging.WARNING,
                            "channel_poll_rejected",
                            channel="weixin",
                            attempt=failures,
                            result_code=result,
                        )
                        await _wait(
                            stop,
                            self.config.reconnect_max_seconds if failures >= 3 else 2,
                        )
                        if failures >= 3:
                            failures = 0
                        continue
                    failures = 0
                    messages = response.get("msgs")
                    if not isinstance(messages, list):
                        messages = []
                    for raw in messages:
                        if not isinstance(raw, dict):
                            continue
                        await self._accept(raw, on_event, session)
                    cursor = response.get("get_updates_buf")
                    if (
                        isinstance(cursor, str)
                        and cursor
                        and cursor != self.state.get_updates_buf
                    ):
                        self.state.get_updates_buf = cursor
                        self.state.save(self.config.state_path)
            finally:
                self._ready.clear()
                await self._notify(api, "notifystop")
                self._session = None

    async def _accept(
        self,
        raw: dict[str, Any],
        on_event: Callable[[IncomingMessage | OwnerInputStatus], Awaitable[None]],
        session: aiohttp.ClientSession,
    ) -> None:
        state = self.state
        assert state is not None
        if raw.get("group_id") or str(raw.get("from_user_id") or "") != state.user_id:
            return
        message_id = raw.get("message_id") or raw.get("client_id") or raw.get("seq")
        if message_id in (None, ""):
            log_event(
                logger,
                logging.WARNING,
                "channel_message_dropped",
                channel="weixin",
                reason="missing_message_id",
            )
            return
        context_token = raw.get("context_token")
        if isinstance(context_token, str) and context_token:
            state.context_token = context_token
            state.save(self.config.state_path)
        segments = await self._segments(raw, session, str(message_id))
        text = render_segments(segments)
        if not text:
            return
        created = raw.get("create_time_ms")
        occurred_at = (
            float(created) / 1000 if isinstance(created, (int, float)) else time.time()
        )
        await on_event(
            IncomingMessage(
                event_id=f"weixin:{state.account_id}:{message_id}",
                message_id=str(message_id),
                text=text,
                occurred_at=occurred_at,
                received_at=time.time(),
                segments=segments,
                channel=self.name,
            )
        )

    async def _segments(
        self,
        raw: dict[str, Any],
        session: aiohttp.ClientSession,
        message_id: str,
    ) -> tuple[dict[str, Any], ...]:
        items = raw.get("item_list")
        if not isinstance(items, list):
            return ()
        segments: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            reference = item.get("ref_msg")
            if isinstance(reference, dict):
                quoted = reference.get("message_item")
                quoted_segments: list[dict[str, Any]] = []
                if isinstance(quoted, dict):
                    quoted_segments.extend(
                        await self._item_segment(
                            quoted, session, f"{message_id}-quote-{index}"
                        )
                    )
                segments.append(
                    {
                        "type": "reply",
                        "data": {
                            "title": str(reference.get("title") or ""),
                            "_quoted": {"segments": quoted_segments},
                        },
                    }
                )
            segments.extend(
                await self._item_segment(item, session, f"{message_id}-{index}")
            )
        return tuple(segments)

    async def _item_segment(
        self,
        item: dict[str, Any],
        session: aiohttp.ClientSession,
        stem: str,
    ) -> list[dict[str, Any]]:
        kind = int(item.get("type") or 0)
        if kind == MESSAGE_TEXT:
            value = item.get("text_item")
            text = str(value.get("text") or "") if isinstance(value, dict) else ""
            return [{"type": "text", "data": {"text": text}}] if text else []
        if kind not in {MESSAGE_IMAGE, MESSAGE_VOICE, MESSAGE_FILE, MESSAGE_VIDEO}:
            return []
        value = item.get(
            {2: "image_item", 3: "voice_item", 4: "file_item", 5: "video_item"}[kind]
        )
        value = value if isinstance(value, dict) else {}
        if kind == MESSAGE_VOICE:
            converted = await self.convert_voice(
                IncomingVoice(native_text=str(value.get("text") or ""))
            )
        else:
            converted = None
        if converted:
            return [
                {
                    "type": "text",
                    "data": {
                        "text": converted,
                        "source": "weixin_voice_transcription",
                    },
                }
            ]
        segment_type = {2: "image", 3: "record", 4: "file", 5: "video"}[kind]
        try:
            path, mime, original = await download_item(
                session,
                item,
                self.config.media_dir,
                stem,
                self.config.media_max_bytes,
            )
            data = {"file": str(path), "name": original, "media_type": mime}
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ValueError,
            OSError,
        ) as error:
            log_event(
                logger,
                logging.WARNING,
                "channel_media_failure",
                channel="weixin",
                media_type=segment_type,
                message_id=stem,
                error_type=type(error).__name__,
            )
            data = {
                "name": str(value.get("file_name") or segment_type),
                "unavailable": True,
            }
        return [{"type": segment_type, "data": data}]

    async def convert_voice(self, voice: IncomingVoice) -> str | None:
        return voice.native_text.strip() or None

    async def send_message(self, payload: dict[str, Any]) -> str:
        if payload.get("action") == "forward":
            raise SendRejected("Weixin does not support outbound forwarded messages")
        state = self.state
        session = self._session
        if (
            state is None
            or session is None
            or not self._ready.is_set()
            or self._paused_until > time.monotonic()
            or not state.context_token
        ):
            raise NotConnected("Weixin has no active owner session")
        segments = payload.get("segments")
        if not isinstance(segments, list) or not segments:
            raise SendRejected("Weixin message has no segments")
        identifiers: list[str] = []
        async with self._send_lock:
            api = WeixinAPI(
                session, state.base_url, state.token, self.config.send_timeout_seconds
            )
            for segment in segments:
                if not isinstance(segment, dict):
                    raise SendRejected("Weixin message contains an invalid segment")
                try:
                    item = await self._outbound_item(segment, api, session)
                    identifier = await self._send_item(api, item)
                    identifiers.append(identifier)
                except (NotConnected, SendRejected, AmbiguousSend):
                    if identifiers:
                        raise AmbiguousSend(
                            "Weixin partially sent a multi-item message"
                        ) from None
                    raise
                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    RuntimeError,
                ) as error:
                    raise AmbiguousSend("Weixin send result is unknown") from error
                except (OSError, ValueError) as error:
                    if identifiers:
                        raise AmbiguousSend(
                            "Weixin partially sent a multi-item message"
                        ) from error
                    raise SendRejected(str(error)) from error
        return identifiers[-1]

    async def _outbound_item(
        self,
        segment: dict[str, Any],
        api: WeixinAPI,
        session: aiohttp.ClientSession,
    ) -> dict[str, Any]:
        kind = str(segment.get("type") or "")
        data = segment.get("data")
        data = data if isinstance(data, dict) else {}
        if kind == "text":
            text = str(data.get("text") or "").strip()
            if not text:
                raise SendRejected("Weixin text must not be empty")
            return {"type": MESSAGE_TEXT, "text_item": {"text": text}}
        if kind in {"reply", "forward"}:
            raise SendRejected(f"Weixin does not support outbound {kind} segments")
        if kind not in {"image", "video", "file", "audio", "record"}:
            raise SendRejected(
                f"Weixin does not support outbound {kind or 'unknown'} segments"
            )
        source = data.get("file")
        if not isinstance(source, str) or not source:
            raise SendRejected("Weixin media segment requires file")
        content, source_name, source_mime = await read_source(
            session, source, self.config.media_max_bytes
        )
        name = safe_filename(str(data.get("name") or source_name), "attachment.bin")
        wire_kind = kind if kind in {"image", "video"} else "file"
        upload_type = {"image": 1, "video": 2, "file": 3}[wire_kind]
        uploaded = await self._upload(api, session, content, upload_type)
        media = {
            "encrypt_query_param": uploaded["parameter"],
            "aes_key": base64.b64encode(uploaded["key"]).decode("ascii"),
            "encrypt_type": 1,
        }
        if wire_kind == "image":
            return {
                "type": MESSAGE_IMAGE,
                "image_item": {"media": media, "mid_size": uploaded["cipher_size"]},
            }
        if wire_kind == "video":
            return {
                "type": MESSAGE_VIDEO,
                "video_item": {"media": media, "video_size": uploaded["cipher_size"]},
            }
        if not Path(name).suffix:
            extension = _extension_for_mime(str(data.get("media_type") or source_mime))
            name += extension
        return {
            "type": MESSAGE_FILE,
            "file_item": {"media": media, "file_name": name, "len": str(len(content))},
        }

    async def _upload(
        self,
        api: WeixinAPI,
        session: aiohttp.ClientSession,
        content: bytes,
        upload_type: int,
    ) -> dict[str, Any]:
        state = self.state
        assert state is not None
        key = secrets.token_bytes(16)
        filekey = secrets.token_hex(16)
        try:
            response = await api.post(
                "ilink/bot/getuploadurl",
                {
                    "filekey": filekey,
                    "media_type": upload_type,
                    "to_user_id": state.user_id,
                    "rawsize": len(content),
                    "rawfilemd5": hashlib.md5(
                        content, usedforsecurity=False
                    ).hexdigest(),
                    "filesize": padded_size(len(content)),
                    "no_need_thumb": True,
                    "aeskey": key.hex(),
                    "base_info": api.base_info(),
                },
            )
        except WeixinHTTPError as error:
            if 400 <= error.status < 500:
                raise SendRejected(str(error)) from error
            raise AmbiguousSend("Weixin media upload request is uncertain") from error
        result = int(response.get("ret") or response.get("errcode") or 0)
        if result:
            raise SendRejected(
                f"Weixin rejected media upload request with ret={result}"
            )
        full_url = str(response.get("upload_full_url") or "").strip()
        parameter = str(response.get("upload_param") or "").strip()
        url = full_url or (
            f"{CDN_BASE_URL}/upload?encrypted_query_param={quote(parameter, safe='')}"
            f"&filekey={quote(filekey, safe='')}"
            if parameter
            else ""
        )
        if not url or urlparse(url).scheme not in {"http", "https"}:
            raise SendRejected("Weixin did not provide a media upload URL")
        ciphertext = encrypt(content, key)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with session.post(
                    url,
                    data=ciphertext,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as upload:
                    if 400 <= upload.status < 500:
                        await upload.read()
                        raise SendRejected(
                            f"Weixin CDN rejected upload with HTTP {upload.status}"
                        )
                    if upload.status != 200:
                        await upload.read()
                        raise RuntimeError(f"Weixin CDN returned HTTP {upload.status}")
                    download_parameter = upload.headers.get("x-encrypted-param")
                    if download_parameter:
                        return {
                            "parameter": download_parameter,
                            "key": key,
                            "cipher_size": len(ciphertext),
                        }
                    raise RuntimeError("Weixin CDN omitted x-encrypted-param")
            except SendRejected:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as error:
                last_error = error
                if attempt < 2:
                    continue
        raise AmbiguousSend("Weixin CDN upload result is unknown") from last_error

    async def _send_item(self, api: WeixinAPI, item: dict[str, Any]) -> str:
        state = self.state
        assert state is not None
        identifier = f"openclaw-weixin:{int(time.time() * 1000)}-{secrets.token_hex(4)}"
        try:
            response = await api.post(
                "ilink/bot/sendmessage",
                {
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": state.user_id,
                        "client_id": identifier,
                        "message_type": 2,
                        "message_state": 2,
                        "item_list": [item],
                        "context_token": state.context_token,
                    },
                    "base_info": api.base_info(),
                },
            )
        except WeixinHTTPError as error:
            if 400 <= error.status < 500:
                raise SendRejected(str(error)) from error
            raise AmbiguousSend("Weixin send result is unknown") from error
        result = int(response.get("ret") or response.get("errcode") or 0)
        if result == -14:
            self._paused_until = time.monotonic() + 3600
            raise NotConnected("Weixin session is stale")
        if result:
            raise SendRejected(f"Weixin rejected message with ret={result}")
        return identifier

    async def _notify(self, api: WeixinAPI, operation: str) -> None:
        try:
            await api.post(
                f"ilink/bot/msg/{operation}",
                {"base_info": api.base_info()},
                timeout=min(self.config.send_timeout_seconds, 10),
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as error:
            log_event(
                logger,
                logging.DEBUG,
                "channel_notify_failure",
                channel="weixin",
                operation=operation,
                error_type=type(error).__name__,
            )

    def content_blocks(
        self, segments: tuple[dict[str, Any], ...]
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for segment in segments:
            data = segment.get("data")
            if segment.get("type") == "reply" and isinstance(data, dict):
                quoted = data.get("_quoted")
                if isinstance(quoted, dict) and isinstance(
                    quoted.get("segments"), list
                ):
                    blocks.extend(self.content_blocks(tuple(quoted["segments"])))
                continue
            if segment.get("type") != "image" or not isinstance(data, dict):
                continue
            source = data.get("file")
            if not isinstance(source, str):
                continue
            try:
                path = Path(source)
                if path.stat().st_size > self.config.media_max_bytes:
                    continue
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError:
                continue
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": str(
                            data.get("media_type") or media_type(path, "image/jpeg")
                        ),
                        "data": encoded,
                    },
                }
            )
        return blocks

    def workflow_variables(self) -> dict[str, str]:
        state = self.state
        return {
            "owner_id": state.user_id if state else "",
            "weixin_user_id": state.user_id if state else "",
            "weixin_account_id": state.account_id if state else "",
        }


def render_segments(segments: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for segment in segments:
        kind = str(segment.get("type") or "unknown")
        data = segment.get("data")
        data = data if isinstance(data, dict) else {}
        if kind == "text":
            text = str(data.get("text") or "").strip()
            if text:
                parts.append(text)
        elif kind == "reply":
            quoted = data.get("_quoted")
            quoted_segments = (
                quoted.get("segments") if isinstance(quoted, dict) else None
            )
            content = (
                render_segments(quoted_segments)
                if isinstance(quoted_segments, list)
                else ""
            )
            title = str(data.get("title") or "").strip()
            summary = " | ".join(value for value in (title, content) if value)
            parts.append(f"[Weixin quoted message{': ' + summary if summary else ''}]")
        elif kind in {"image", "video", "file", "record"}:
            name = str(data.get("name") or Path(str(data.get("file") or kind)).name)
            state = " unavailable" if data.get("unavailable") else ""
            path = str(data.get("file") or "")
            parts.append(
                f"[Weixin {kind}{state}: name={name}{' path=' + path if path else ''}]"
            )
    return "\n".join(parts).strip()


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=max(0, seconds))
    except TimeoutError:
        pass


def _extension_for_mime(value: str) -> str:
    return {
        "audio/mpeg": ".mp3",
        "audio/wav": ".wav",
        "audio/ogg": ".ogg",
        "audio/silk": ".silk",
    }.get(value.split(";", 1)[0].lower(), ".bin")
