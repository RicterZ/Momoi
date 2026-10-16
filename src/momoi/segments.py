"""Channel-neutral helpers for image segments carried in message payloads."""

from collections.abc import Iterator, Sequence
from typing import Any


def visual_segments(segments: Sequence[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        data = segment.get("data")
        if not isinstance(data, dict):
            continue
        if segment.get("type") == "image" and str(data.get("sub_type", "0")) != "1":
            yield data
        quoted = data.get("_quoted")
        if isinstance(quoted, dict):
            yield from visual_segments(quoted.get("segments") or [])
        for node in data.get("_forward") or []:
            if isinstance(node, dict):
                yield from visual_segments(node.get("segments") or [])


def image_block(media_type: str, data: str) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
