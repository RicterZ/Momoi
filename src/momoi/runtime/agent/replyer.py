"""One bounded expression request, accounted under the parent turn."""
from importlib.resources import files
from datetime import datetime
from zoneinfo import ZoneInfo

from ...integrations.request_context import model_request
from ...observability.context import log_context, new_trace_id, current_log_context
from ...storage import estimate_tokens
from ..transcript.replyer import visible_dialogue


def prompt_text(config, name, fallback):
    path = config.soul_prompt_path.parent / name if config.soul_prompt_path else None
    return (path.read_text(encoding="utf-8").strip() or fallback) if path and path.is_file() else fallback


class Replyer:
    def __init__(self, config, store, provider):
        self.config, self.store, self.provider = config, store, provider

    async def generate(self, call, request):
        soul = prompt_text(self.config, self.config.soul_prompt_path.name if self.config.soul_prompt_path else "SOUL.md", self.config.soul_prompt)
        guide = prompt_text(self.config, "REPLYER.md", files("momoi").joinpath("prompts/replyer.md").read_text(encoding="utf-8"))
        system = soul + "\n\n" + guide
        rows = self.store.replyer_dialogue_rows(request.delivery_channel.name)
        messages = visible_dialogue(rows, timezone=self.config.timezone)
        target = "\n".join(event.text for event in request.current_events)
        tail = (
            f"当前时间：{datetime.now(ZoneInfo(str(self.config.timezone))).isoformat()}\n"
            f"本次回应意图：{call.arguments['intent']}\n必要参考：{call.arguments['reference']}\n"
            f"当前目标消息：{target or '本轮自主活动，由回应意图指定对象和内容'}\n"
            "只输出实际发言，用空行分隔气泡。"
        )
        previous = getattr(getattr(request, "state", None), "last_sent_bubbles", None)
        if previous:
            tail += "\n本轮上一批发言已提交发送（不保证已送达），避免重复：" + str(previous)
        blocks = [{"type": "text", "text": tail}]
        # Reuse the channel attachment adapter, with the same image association as Planner.
        for event in request.current_events:
            if event.segments:
                blocks.append({"type": "text", "text": f"目标消息附件：{event.text}"})
                blocks.extend(request.delivery_channel.content_blocks(event.segments))
        messages.append({"role": "user", "content": blocks})
        parent_call_id = current_log_context().get("call_id", request.call_id if hasattr(request, "call_id") else "")
        with log_context(parent_call_id=parent_call_id, stage="replyer", turn_id=request.turn_id, call_id=new_trace_id(),
                         round=request.round_number, channel=request.delivery_channel.name,
                         tool_call_id=call.id), model_request(thinking_effort=self.config.thinking_stages.get("replyer") or "low"):
            response = await self.provider.complete(system, messages, [])
        usage = response.usage or {}
        self.store.record_turn_usage(request.turn_id, int(usage.get("input", estimate_tokens(system + str(messages)))),
                                     int(usage.get("output", estimate_tokens(str(response.content)))))
        if response.tool_calls:
            raise ValueError("Replyer must produce text, not tool calls")
        text = "\n".join(str(block.get("text", "")) for block in response.content if block.get("type") == "text").strip()
        if not text:
            raise ValueError("Replyer returned empty text")
        return [part.strip() for part in text.split("\n\n") if part.strip()]
