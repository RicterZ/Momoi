"""Decoder discovery; the application host may supply its managed tool resolver."""
import shutil


def text_only_messages(messages):
    """Keep tool pairing and textual evidence while omitting unsupported images."""
    def content(value):
        if not isinstance(value, list):
            return value
        blocks = []
        for block in value:
            if block.get("type") in {"image", "image_url"}:
                blocks.append({"type": "text", "text": "[图片未传入：当前模型不支持多模态，请勿声称已查看图片。]"})
            elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                blocks.append({**block, "content": content(block["content"])})
            else:
                blocks.append(block)
        return blocks
    return [{**message, "content": content(message.get("content"))} for message in messages]


def ffmpeg_executable():
    return shutil.which("ffmpeg") or "ffmpeg"
