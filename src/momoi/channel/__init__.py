import asyncio
import importlib
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from ..models import IncomingMessage


ChannelMessage = str | dict[str, Any]
_SEGMENT_TYPE = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")
_MEDIA_TYPES = {"image", "file", "video", "audio", "record"}


class ChannelError(RuntimeError):
    pass


class NotConnected(ChannelError):
    pass


class AmbiguousSend(ChannelError):
    pass


class SendRejected(ChannelError):
    pass


class Channel(Protocol):
    name: str
    prompt_context: str
    quiet_seconds: float
    max_batch_seconds: float

    async def run(
        self,
        on_message: Callable[[IncomingMessage], Awaitable[None]],
        stop: asyncio.Event,
    ) -> None: ...

    async def send_message(self, payload: dict[str, Any]) -> str: ...

    def content_blocks(self, segments: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]: ...

    def workflow_variables(self) -> dict[str, str]: ...


def load_channel_config(name: str, value: object, workspace: Path) -> Any:
    loader = getattr(_plugin(name), "load_config", None)
    if not callable(loader):
        raise ValueError(f"channel plugin has no config loader: {name}")
    return loader(value, workspace)


def create_channel(config: Any) -> Channel:
    name = str(getattr(config, "plugin", ""))
    factory = getattr(_plugin(name), "create_channel", None)
    if not callable(factory):
        raise ValueError(f"channel plugin has no factory: {name}")
    return factory(config)


async def login_channel(config: Any) -> None:
    name = str(getattr(config, "plugin", ""))
    login = getattr(_plugin(name), "login", None)
    if not callable(login):
        raise ValueError(f"channel plugin does not support login: {name}")
    await login(config)


def _plugin(name: str) -> Any:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name):
        raise ValueError("channel.plugin is invalid")
    module_name = f"{__name__}.{name}"
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name == module_name:
            raise ValueError(f"unsupported channel plugin: {name}") from None
        raise


def normalize_channel_message(value: ChannelMessage) -> dict[str, Any]:
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("empty_message")
        return {
            "action": "message",
            "segments": [{"type": "text", "data": {"text": value.strip()}}],
        }
    if not isinstance(value, dict):
        raise ValueError("message_must_be_text_or_object")
    if value.get("action") == "message":
        return {"action": "message", "segments": _normalize_segments(value.get("segments"))}
    if value.get("action") == "forward":
        nodes = value.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("forward_must_be_a_non_empty_array")
        return {"action": "forward", "nodes": [_normalize_node(node) for node in nodes]}
    if "segments" in value:
        return {"action": "message", "segments": _normalize_segments(value["segments"])}
    if "forward" in value:
        nodes = value["forward"]
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("forward_must_be_a_non_empty_array")
        return {"action": "forward", "nodes": [_normalize_node(node) for node in nodes]}
    raise ValueError("message_object_requires_segments_or_forward")


def render_channel_message(message: dict[str, Any]) -> str:
    if message.get("action") == "forward":
        rendered = [
            f"{node['data']['nickname']}: {_render_segments(node['data']['content'])}"
            for node in message.get("nodes") or []
        ]
        return "[forwarded message]\n" + "\n".join(rendered)
    return _render_segments(message.get("segments") or [])


def media_path(message: dict[str, Any]) -> str | None:
    if message.get("action") != "message":
        return None
    segments = message.get("segments") or []
    if len(segments) != 1 or segments[0].get("type") not in _MEDIA_TYPES:
        return None
    value = segments[0].get("data", {}).get("file")
    if not isinstance(value, str) or value.startswith(("http://", "https://", "base64://")):
        return None
    return str(Path(value).expanduser().resolve())


def _normalize_segments(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("segments_must_be_a_non_empty_array")
    normalized: list[dict[str, Any]] = []
    for segment in value:
        if not isinstance(segment, dict):
            raise ValueError("invalid_segment")
        kind = segment.get("type")
        data = segment.get("data")
        if not isinstance(kind, str) or not _SEGMENT_TYPE.fullmatch(kind):
            raise ValueError("invalid_segment_type")
        if not isinstance(data, dict):
            raise ValueError("invalid_segment_data")
        data = dict(data)
        if kind == "text":
            text = data.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("invalid_text_segment")
            if re.search(r"\n\s*\n", text):
                raise ValueError("blank_lines_must_be_separate_messages")
            data["text"] = text.strip()
        if kind in _MEDIA_TYPES and not isinstance(data.get("file"), str):
            raise ValueError("media_segment_requires_file")
        normalized.append({"type": kind, "data": data})
    return normalized


def _normalize_node(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("invalid_forward_node")
    data = value.get("data") if value.get("type") == "node" else value
    if not isinstance(data, dict):
        raise ValueError("invalid_forward_node")
    nickname = data.get("nickname")
    if not isinstance(nickname, str) or not nickname.strip():
        raise ValueError("forward_node_requires_nickname")
    content = data.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "data": {"text": content}}]
    return {
        "type": "node",
        "data": {
            "user_id": str(data.get("user_id") or "0"),
            "nickname": nickname.strip(),
            "content": _normalize_segments(content),
        },
    }


def _render_segments(segments: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for segment in segments:
        kind = str(segment.get("type") or "unknown")
        data = segment.get("data")
        data = data if isinstance(data, dict) else {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "reply":
            parts.append(f"[reply to message_id={data.get('id', 'unknown')}]")
        else:
            parts.append(f"[{kind}: {_compact(data)}]")
    return "\n".join(part for part in parts if part).strip()


def _compact(value: object, limit: int = 4000) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"
