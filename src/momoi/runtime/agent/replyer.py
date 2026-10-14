"""One bounded expression request, accounted under the parent turn."""
from importlib.resources import files
from datetime import datetime
from zoneinfo import ZoneInfo
import json

from ...integrations.request_context import model_request
from ...observability.context import log_context, new_trace_id, current_log_context
from ...memory.text import estimate_tokens
from ...storage.delivery.emotions import EMOTION_REACTION_POLICY
from ...storage.delivery.actions import QQ_POKE_MARKER, QQ_POKE_DESCRIPTION
from ..transcript.replyer import visible_dialogue
from ..turn_support import context_data_message


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
        # Active inputs/progress are not archived until commit_turn. Project them
        # here without changing the persisted history window or commit lifecycle.
        events = [event for event in request.current_events
                  if not event.delivery_context.get("channel_notice")
                  and (not callable(getattr(request.delivery_channel, "message_current", None))
                       or request.delivery_channel.message_current(event))]
        interjection = self.store._db.execute(
            "SELECT payload_json FROM turn_journal WHERE turn_id=? AND item_type='owner_interjection' ORDER BY sequence DESC LIMIT 1",
            (request.turn_id,),
        ).fetchone()
        target_ids = set(json.loads(interjection["payload_json"]).get("event_ids", [])) if interjection else {event.event_id for event in events}
        active_rows = []
        archived = self.store._db.execute(
            "SELECT role, source_event_ids_json, outbox_id FROM messages WHERE turn_id=?",
            (request.turn_id,),
        ).fetchall()
        archived_events = {identifier for row in archived if row["role"] == "user"
                           for identifier in json.loads(row["source_event_ids_json"])}
        archived_outbox = {row["outbox_id"] for row in archived if row["outbox_id"] is not None}
        for event in events:
            if event.event_id not in archived_events:
                active_rows.append({"id": event.event_id, "role": "user", "content": event.text,
                                    "created_at": event.received_at, "turn_id": request.turn_id,
                                    "delivery_state": "delivered", "_event": event})
        for row in self.store._db.execute(
            """SELECT o.id, p.text, COALESCE(json_extract(
                   CASE WHEN json_valid(o.payload_json) THEN o.payload_json ELSE '{}' END,
                   '$._sent_at'), p.created_at) AS created_at FROM turn_progress p JOIN outbox o
               ON o.dedupe_key='turn:' || p.turn_id || ':progress:' || p.tool_call_id || ':' || p.part_index
               WHERE p.turn_id=? AND o.state='sent' AND o.target_channel=?""",
            (request.turn_id, request.delivery_channel.name),
        ):
            if row["id"] not in archived_outbox:
                active_rows.append({"id": row["id"], "role": "assistant", "content": row["text"],
                                    "created_at": row["created_at"], "turn_id": request.turn_id,
                                    "delivery_state": "delivered"})
        rows = sorted([*rows, *active_rows], key=lambda row: row["created_at"])
        turn_ids = list(dict.fromkeys(str(row["turn_id"]) for row in rows))
        # Read the shared snapshot/deltas without moving Planner's compaction boundary.
        memory_state = self.store.transcript_memory_context(turn_ids, track_boundary=False)
        memory_prefix = context_data_message(
            ("long_term_memories", self.store._memory_context(memory_state["snapshot"].values())),
            ("memory_overrides", "\n".join(memory_state["snapshot_overrides"].values())),
            required=True,
        )
        dialogue = visible_dialogue(rows, timezone=self.config.timezone, memory_events=memory_state["events"])
        dialogue_indexes = [i for i, message in enumerate(dialogue) if "_memory_change" not in message]
        for row, index in zip(rows, dialogue_indexes):
            message = dialogue[index]
            event = row.get("_event")
            if event:
                label = "本次最新目标" if event.event_id in target_ids else "本turn历史，已经进入过对话，不重新回应"
                text = f"[{label} event_id={event.event_id}] " + message["content"]
                if event.segments and event.event_id in target_ids:
                    dialogue[index] = {"role": "user", "content": [{"type": "text", "text": text},
                        *request.delivery_channel.content_blocks(event.segments)]}
                else:
                    dialogue[index] = {"role": "user", "content": text}
        messages = [memory_prefix, *dialogue]
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
        expression = (
            "只输出适合朗读的实际发言，用空行分隔气泡；使用自然口语，不包含 Markdown、颜文字、表情标记、动作标记或媒体路径。"
            if mode == "voice" else "只输出实际发言，用空行分隔气泡。"
        )
        tail = (
            f"当前时间：{datetime.now(ZoneInfo(str(self.config.timezone))).isoformat()}\n"
            f"发送形式：{'语音' if mode == 'voice' else '文字'}\n本次回应意图：{call.arguments['intent']}\n必要参考：{call.arguments['reference']}\n"
            f"本次目标消息ID：{', '.join(sorted(target_ids)) or '本轮自主活动，由回应意图指定对象和内容'}\n"
            "只处理本次最新目标；历史对话仅用于衔接，不重新回应旧图片或旧消息，不补发被打断而未送达的话。遵循本次回应意图。\n"
            + expression
        )
        if call.arguments.get("attachments"):
            tail += "\n本次附件由发送层原样发送，发言无需重述附件路径或生成媒体指令。"
        blocks = [{"type": "text", "text": tail}]
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
            if QQ_POKE_DESCRIPTION in bubble or "[戳一戳用户]" in bubble:
                raise ValueError("戳一戳的历史动作说明不能作为文字发送，请用 qq://poke 单独作为一个气泡")
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
