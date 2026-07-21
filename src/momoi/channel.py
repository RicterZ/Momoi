import json
import re
from pathlib import Path
from typing import Any


ChannelMessage = str | dict[str, Any]
_SEGMENT_TYPE = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")
_MEDIA_TYPES = {"image", "file", "video", "record"}


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


def render_segments(segments: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> str:
    parts: list[str] = []
    for segment in segments:
        kind = str(segment.get("type") or "unknown")
        data = segment.get("data")
        data = data if isinstance(data, dict) else {}
        if kind == "text":
            value = str(data.get("text") or "")
            if value:
                parts.append(value)
            continue
        if kind == "image":
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
    segments: list[dict[str, Any]] | tuple[dict[str, Any], ...]
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
        segments = _normalize_segments(value["segments"])
        return {"action": "message", "segments": segments}
    if "forward" in value:
        nodes = value["forward"]
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("forward_must_be_a_non_empty_array")
        return {"action": "forward", "nodes": [_normalize_node(node) for node in nodes]}
    raise ValueError("message_object_requires_segments_or_forward")


def render_channel_message(message: dict[str, Any]) -> str:
    if message.get("action") == "forward":
        nodes = message.get("nodes") or []
        rendered = [
            f"{node['data']['nickname']}: {render_segments(node['data']['content'])}"
            for node in nodes
        ]
        return "[QQ forwarded message]\n" + "\n".join(rendered)
    return render_segments(message.get("segments") or [])


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
        if kind == "reply" and not isinstance(data.get("id"), (str, int)):
            raise ValueError("reply_segment_requires_id")
        if kind in {"json", "xml"} and isinstance(data.get("data"), dict):
            data["data"] = json.dumps(data["data"], ensure_ascii=False, separators=(",", ":"))
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
    header = (
        f"[QQ quoted message_id={message_id} from {sender_name}({sender_id})]"
    )
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
            detail = next(
                (value for value in meta.values() if isinstance(value, dict)), {}
            )
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
    label = (
        data.get("summary")
        or data.get("text")
        or raw.get("faceText")
        or raw.get("vaspokeName")
    )
    identifier = data.get("id") or data.get("emoji_id") or "unknown"
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
