"""Tool-free phone expression over a bounded projection of real dialogue."""
import re
from importlib.resources import files
from datetime import datetime
from zoneinfo import ZoneInfo

from .replyer import ReplyerBase, prompt_text
from ...integrations.request_context import model_request
from ...observability.context import log_context, new_trace_id
from ...memory.text import estimate_tokens
from ..turn_support import context_data_message




class PhoneReplyer(ReplyerBase):
    def messages(self, events, turn_id):
        background = context_data_message(
            ("long_term_memories", self.store.always_memory_context()),
            ("current_state", self.store.scoped_memory_context("current_state")),
            ("self_state", self.store.self_state_context(affinity_enabled=self.config.affinity_enabled)),
            ("runtime_directives", "当前时间：" + datetime.now(ZoneInfo(str(self.config.timezone))).isoformat()),
            required=True,
        )
        rows = self.store.replyer_dialogue_rows("qq_call", limit=24)
        selected, used = [], 0
        for row in reversed(rows):
            if row["turn_id"] == turn_id:
                continue
            text = str(row["content"])
            size = estimate_tokens(text)
            if used + size > 6000:
                break
            selected.append({"role": row["role"], "content": text})
            used += size
        return [background, *reversed(selected),
                *({"role": "user", "content": event.text} for event in events)]

    async def generate(self, events, turn_id):
        guide = prompt_text(self.config, "PHONE_REPLYER.md", files("momoi").joinpath("prompts/phone_replyer.md").read_text(encoding="utf-8"))
        system = self.expression_prompt() + "\n\n" + guide
        messages = self.messages(events, turn_id)
        with log_context(stage="phone_replyer", turn_id=turn_id, channel="qq_call", call_id=new_trace_id()), model_request(
            thinking_effort=self.config.thinking_stages.get("phone_replyer", "off")
        ):
            response = await self.provider.complete(system, messages, [])
        usage = response.usage or {}
        self.store.record_turn_usage(turn_id, int(usage.get("input", estimate_tokens(system + str(messages)))), int(usage.get("output", estimate_tokens(str(response.content)))))
        if response.tool_calls:
            raise ValueError("phone replyer returned unsupported tools")
        text = "\n".join(str(block.get("text", "")) for block in response.content
                         if block.get("type") == "text").strip()
        # SOUL may teach chat-only actions; never pass those markers to TTS.
        text = re.sub(r"(?:emotion|qq)://[^\s]+", "", text).strip()
        if text == "[WAIT]":
            return ""
        if not text:
            raise ValueError("phone replyer returned empty text")
        return text
