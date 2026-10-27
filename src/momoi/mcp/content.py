"""Move MCP image bytes into the existing image store before text projection."""

import base64
import binascii
import copy
import io
import warnings

from PIL import Image

from ..storage.conversation.images import MAX_IMAGE_BYTES


def store_content_images(raw, store):
    """Return a byte-free result and ordered, durable image references.

    Only standard MCP content blocks are interpreted. Tool text and structured
    content remain untrusted data, never instructions or attachment sources.
    """
    raw = copy.deepcopy(raw)
    body = raw.get("result")
    if not isinstance(body, dict) or not isinstance(body.get("content"), list):
        return raw, []
    references = []
    blocks = []
    has_images = False
    for block in body["content"]:
        if not isinstance(block, dict) or block.get("type") != "image":
            blocks.append(block)
            continue
        mime = block.get("mimeType")
        has_images = True
        encoded = block.get("data")
        identifier = None
        try:
            if mime not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
                raise ValueError("unsupported MIME")
            if not isinstance(encoded, str) or len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 4:
                raise ValueError("invalid size")
            data = base64.b64decode(encoded, validate=True)
            if not data or len(data) > MAX_IMAGE_BYTES:
                raise ValueError("invalid size")
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    if Image.MIME.get(image.format) != mime:
                        raise ValueError("MIME mismatch")
                    image.verify()
            segments = [{"type": "image", "data": {
                "file": "base64://" + encoded, "media_type": mime,
            }}]
            # register_images participates in its caller's transaction. MCP
            # results have no incoming-event transaction, so commit explicitly.
            with store._db:
                identifiers = store.register_images(segments)
            if identifiers:
                identifier = identifiers[0]
        except (ValueError, OSError, SyntaxError, binascii.Error, Image.DecompressionBombError,
                Image.DecompressionBombWarning):
            pass
        if identifier:
            if identifier not in references:
                references.append(identifier)
            text = (f"[MCP image attachment id={identifier}; MIME={mime}; "
                    "use read_image to inspect again after history reload.]")
        else:
            text = ("[MCP image unavailable: invalid image, unsupported MIME, or size "
                    "exceeds 20 MiB. Do not claim to have viewed it.]")
            raw["image_warning"] = text
        blocks.append({"type": "text", "text": text})
    body["content"] = blocks
    if has_images and "structuredContent" in body:
        # mcp_body normally prefers structuredContent. Mixed media must retain
        # the accompanying text as well as any structured business evidence.
        raw["structured_result"] = body.pop("structuredContent")
    return raw, references


def attachment_blocks(store, call_id, references):
    blocks = []
    for identifier in references:
        image = store.read_image(identifier)
        blocks.append({"type": "text", "text": (
            f"[Untrusted MCP tool image; tool_call_id={call_id}; image_id={identifier}]"
            if image else f"[MCP image unavailable: {identifier}; do not claim to have viewed it.]"
        )})
        if image:
            blocks.append(image)
    return blocks
