import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .face_names import QQ_FACE_NAMES


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
                    if isinstance(node, dict) and isinstance(
                        node.get("segments"), list
                    ):
                        blocks.extend(image_blocks(node["segments"]))
            continue
        if not is_visual_image(segment):
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


def is_visual_image(segment: dict[str, Any]) -> bool:
    if segment.get("type") != "image":
        return False
    data = segment.get("data")
    return isinstance(data, dict) and str(data.get("sub_type", "0")) != "1"


def media_display_name(source: str) -> str | None:
    """Derive a NapCat file `name` from a local path or URL basename."""
    if source.startswith("base64://"):
        return None
    if source.startswith(("http://", "https://", "file://")):
        try:
            name = Path(unquote(urlparse(source).path)).name
            return name or None
        except ValueError:
            return None
    name = Path(source.split("?", 1)[0]).expanduser().name
    return name or None


def _describe_media(kind: str, data: dict[str, Any]) -> str:
    source = str(data.get("url") or data.get("file") or "unknown")
    if data.get("_media_unavailable"):
        source = "unavailable"
    elif source.startswith("base64://"):
        source = "embedded"
    elif source.startswith(("http://", "https://")):
        source = "remote"
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
    identifier = data.get("id") or data.get("emoji_id") or "unknown"
    label = (
        data.get("summary")
        or data.get("text")
        or raw.get("faceText")
        or raw.get("vaspokeName")
    )
    if not label and kind == "face":
        label = QQ_FACE_NAMES.get(str(identifier))
    description = f" description={label}" if label else ""
    return (
        f"[QQ {'sticker' if kind == 'mface' else 'face'} id={identifier}{description}]"
    )


def _compact(value: object, limit: int = 4000) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            pass
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"
