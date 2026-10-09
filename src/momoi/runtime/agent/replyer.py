"""One bounded expression request, accounted under the parent turn."""
from importlib.resources import files
from datetime import datetime
from zoneinfo import ZoneInfo

from ...integrations.request_context import model_request
from ...observability.context import log_context, new_trace_id, current_log_context
from ...memory.text import estimate_tokens
from ...storage.delivery.emotions import EMOTION_REACTION_POLICY
from ...storage.delivery.actions import QQ_POKE_MARKER, QQ_POKE_POLICY
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
        rows = self.store.replyer_history_rows(getattr(request.delivery_channel, "dialogue_channel", request.delivery_channel.name))
        messages = visible_dialogue(rows, timezone=self.config.timezone)
        target = "\n".join(event.text for event in request.current_events
                           if not event.delivery_context.get("channel_notice")
                           and (not callable(getattr(request.delivery_channel, "message_current", None))
                                or request.delivery_channel.message_current(event)))
        mode = call.arguments.get("mode", "text")
        if mode not in {"text", "voice"}:
            raise ValueError("invalid reply mode")
        quote_id = call.arguments.get("reply_to_message_id")
        if quote_id is not None:
            if mode != "text" or request.delivery_channel.name != "napcat":
                raise ValueError("quote replies require QQ text mode")
            current_target = any(event.message_id == quote_id and event.channel == "napcat"
                       and not event.delivery_context.get("channel_notice")
                       and request.delivery_channel.message_current(event)
                       for event in request.current_events)
            historical_target = getattr(request.delivery_channel, "is_quote_target", lambda _: False)(quote_id)
            if not (current_target or historical_target):
                raise ValueError("quote target must be a known, unrecalled QQ message")
        emotions = self.store.emotion_context()
        if emotions.strip():
            system += "\n\n<emotion_catalog>\n" + emotions + "\n</emotion_catalog>\n" + EMOTION_REACTION_POLICY
            system += "\n文字模式选用表情时，将 emotion:// 标识单独作为一个气泡，用空行与文字分隔；只能使用目录中的标识。语音模式只生成朗读文本，不输出表情。"
        if getattr(request.delivery_channel, "dialogue_channel", request.delivery_channel.name) == "napcat":
            system += "\n\n" + QQ_POKE_POLICY
        expression = (
            "只输出适合朗读的实际发言，用空行分隔气泡；使用自然口语，不包含 Markdown、颜文字、表情标记、动作标记或媒体路径。"
            if mode == "voice" else "只输出实际发言，用空行分隔气泡。"
        )
        tail = (
            f"当前时间：{datetime.now(ZoneInfo(str(self.config.timezone))).isoformat()}\n"
            f"发送形式：{'语音' if mode == 'voice' else '文字'}\n本次回应意图：{call.arguments['intent']}\n必要参考：{call.arguments['reference']}\n"
            f"当前目标消息：{target or '本轮自主活动，由回应意图指定对象和内容'}\n"
            + expression
        )
        if call.arguments.get("attachments"):
            tail += "\n本次附件由发送层原样发送，发言无需重述附件路径或生成媒体指令。"
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
        bubbles = [part.strip() for part in text.split("\n\n") if part.strip()]
        for bubble in bubbles:
            if QQ_POKE_MARKER not in bubble:
                continue
            if bubble != QQ_POKE_MARKER:
                raise ValueError("poke directive must be a standalone bubble")
            if mode != "text" or not callable(getattr(request.delivery_channel, "poke_owner", None)):
                raise ValueError("poke is unavailable in this reply mode or channel")
        if quote_id is not None:
            for index, bubble in enumerate(bubbles):
                if bubble.startswith("emotion://") or bubble == QQ_POKE_MARKER:
                    continue
                bubbles[index] = {"segments": [
                    {"type": "reply", "data": {"id": quote_id}},
                    {"type": "text", "data": {"text": bubble}},
                ]}
                break
            else:
                raise ValueError("quote reply requires a text bubble")
        return bubbles
