"""Replace images with recognized evidence only for a text-only primary model."""

import asyncio
import hashlib
import json
from collections import OrderedDict
from importlib.resources import files

from ..llm.errors import ProviderResponseError
from ..observability.context import current_log_context, log_context, new_trace_id


class ImageRoutingModel:
    def __init__(self, primary, vision, *, prompt_path=None):
        self.primary = primary
        self.vision = vision
        self.usage_sink = primary.usage_sink
        self.thinking_sink = primary.thinking_sink
        self.usage_parser = primary.usage_parser
        self.request_metrics_sink = getattr(primary, "request_metrics_sink", None)
        self._summaries = OrderedDict()
        self._lock = asyncio.Lock()
        self.prompt_path = prompt_path

    def _prompt(self):
        if self.prompt_path is not None:
            try:
                text = self.prompt_path.read_text(encoding="utf-8").strip()
                if text:
                    return text
            except FileNotFoundError:
                pass
        return files("momoi").joinpath("prompts/image_recognition.md").read_text(encoding="utf-8").strip()

    def __getattr__(self, name):
        return getattr(self.primary, name)

    async def _recognize(self, image):
        prompt = self._prompt()
        key = hashlib.sha256((prompt + json.dumps(image, sort_keys=True)).encode()).hexdigest()
        async with self._lock:
            if key in self._summaries:
                self._summaries.move_to_end(key)
                return self._summaries[key]
            parent = current_log_context()
            with log_context(stage="image_recognition", call_id=new_trace_id(),
                             parent_call_id=parent.get("call_id")):
                response = await self.vision.complete(
                    prompt,
                    [{"role": "user", "content": [image]}], [],
                )
            text = "\n".join(block.get("text", "") for block in response.content if block.get("type") == "text").strip()
            if not text:
                raise ProviderResponseError("图片识别模型未返回识别结果")
            summary = {"type": "text", "text": f"[图片识别结果（辅助模型提供，并非主模型直接查看）]\n{text}\n[/图片识别结果]"}
            self._summaries[key] = summary
            if len(self._summaries) > 128:
                self._summaries.popitem(last=False)
            return summary

    async def _content(self, value):
        if not isinstance(value, list):
            return value
        blocks = []
        for block in value:
            if block.get("type") in {"image", "image_url"}:
                blocks.append(await self._recognize(block))
            elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                blocks.append({**block, "content": await self._content(block["content"])})
            else:
                blocks.append(block)
        return blocks

    async def complete(self, system, messages, tools=None, *, require_tool=False, required_tool=None):
        for model in (self.primary, self.vision):
            model.usage_sink = self.usage_sink
            model.thinking_sink = self.thinking_sink
            if hasattr(model, "request_metrics_sink"):
                model.request_metrics_sink = self.request_metrics_sink
        self.primary.usage_parser = self.usage_parser
        converted = [{**message, "content": await self._content(message.get("content"))} for message in messages]
        return await self.primary.complete(system, converted, tools,
                                           require_tool=require_tool, required_tool=required_tool)
