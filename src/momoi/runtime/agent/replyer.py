"""Bounded expression and interaction tools, accounted under the parent turn."""
from importlib.resources import files
from datetime import datetime
from zoneinfo import ZoneInfo
import json
from xml.sax.saxutils import escape

from ...integrations.request_context import model_request
from ...observability.context import log_context, new_trace_id, current_log_context
from ...memory.text import estimate_tokens
from ...storage.conversation.transcripts import mark_recalled_messages
from ...storage.delivery.emotions import EMOTION_REACTION_POLICY
from ...storage.delivery.actions import QQ_POKE_MARKER, QQ_POKE_DESCRIPTION
from ..transcript.replyer import visible_dialogue
from ..turn_support import context_data_message, tool_result_block
from ..tool_contracts.qq_message import QQ_POKE_SPEC
from .protocol import assistant_history_message


def prompt_text(config, name, fallback):
    path = config.soul_prompt_path.parent / name if config.soul_prompt_path else None
    return (path.read_text(encoding="utf-8").strip() or fallback) if path and path.is_file() else fallback


def response_text(content):
    return "\n".join(str(block.get("text", "")) for block in content if block.get("type") == "text").strip()


def _finalize_bubbles(text, *, attachments, mode, channel, quote_id):
    bubbles = [part.strip() for part in text.split("\n\n") if part.strip()]
    sources = {
        str(segment.get("data", {}).get("file"))
        for attachment in attachments or []
        if isinstance(attachment, dict)
        for segment in attachment.get("segments") or []
        if isinstance(segment, dict) and isinstance(segment.get("data"), dict)
        and segment["data"].get("file")
    }
    # The sending layer delivers attachments; a bubble quoting one is a
    # textual stand-in for the media and must not be sent as words.
    bubbles = [bubble for bubble in bubbles if not any(source in bubble for source in sources)]
    for bubble in bubbles:
        if QQ_POKE_DESCRIPTION in bubble or "[戳一戳用户]" in bubble:
            raise ValueError("戳一戳的历史动作说明不能作为文字发送，请用 qq://poke 单独作为一个气泡")
        if QQ_POKE_MARKER not in bubble:
            continue
        if bubble != QQ_POKE_MARKER:
            raise ValueError("poke directive must be a standalone bubble")
        if mode != "text" or not callable(getattr(channel, "poke_owner", None)):
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


BUBBLE_STREAK_LIMIT = 2


def reply_key(row, dedupe_keys):
    """Progress bubbles of one reply call share `...:progress:<call>:<n>`."""
    parts = str(dedupe_keys.get(row.get("outbox_id")) or "").split(":")
    call = parts[parts.index("progress") + 1] if "progress" in parts[:-1] else "final"
    return str(row["turn_id"]), call


def bubble_streak(rows, dedupe_keys=None):
    """Return (replies, bubbles) when the latest replies repeat one bubble count."""
    counts = {}
    for row in rows:
        if row["role"] == "assistant":
            key = reply_key(row, dedupe_keys or {})
            counts[key] = counts.get(key, 0) + 1
    replies = list(counts.values())
    if not replies:
        return None
    streak = 0
    for count in reversed(replies):
        if count != replies[-1]:
            break
        streak += 1
    return (streak, replies[-1]) if streak >= BUBBLE_STREAK_LIMIT else None


class ReplyerBase:
    def __init__(self, config, store, provider):
        self.config, self.store, self.provider = config, store, provider

    def expression_prompt(self):
        soul = prompt_text(self.config, self.config.soul_prompt_path.name if self.config.soul_prompt_path else "SOUL.md", self.config.soul_prompt)
        return soul


class Replyer(ReplyerBase):
    def interaction_rows(self, turn_ids, rows, reset_at):
        """Successful user-facing actions live in tool_audit, not messages."""
        floor = max(reset_at, rows[0]["created_at"] if rows else 0)
        actions = []
        for row in self.store.replyer_interaction_rows(turn_ids, floor):
            result = json.loads(row["result_json"])
            actions.append({"id": f"history_action_{row['id']}", "role": "action", "name": row["tool_name"],
                            "input": {}, "result": result, "created_at": row["completed_at"],
                            "turn_id": row["turn_id"]})
        return actions

    async def generate(self, call, request, *, poke_already_sent=False):
        soul = self.expression_prompt()
        guide = prompt_text(self.config, "REPLYER.md", files("momoi").joinpath("prompts/replyer.md").read_text(encoding="utf-8"))
        system = "\n\n".join(part for part in (soul, guide) if part)
        history_channel = getattr(request.delivery_channel, "dialogue_channel", request.delivery_channel.name)
        rows = self.store.replyer_history_rows(history_channel)
        reset_at = getattr(self.store, "replyer_reset_at", lambda _: 0)(history_channel)
        # Active inputs/progress are not archived until commit_turn. Project them
        # here without changing the persisted history window or commit lifecycle.
        events = [event for event in request.current_events
                  if event.received_at > reset_at
                  and not event.delivery_context.get("channel_notice")
                  and (not callable(getattr(request.delivery_channel, "message_current", None))
                       or request.delivery_channel.message_current(event))]
        interjection = self.store.replyer_owner_interjection(request.turn_id)
        target_ids = set(json.loads(interjection["payload_json"]).get("event_ids", [])) if interjection else {event.event_id for event in events}
        active_rows = []
        archived = self.store.replyer_archived_rows(request.turn_id)
        archived_events = {identifier for row in archived if row["role"] == "user"
                           for identifier in json.loads(row["source_event_ids_json"])}
        archived_outbox = {row["outbox_id"] for row in archived if row["outbox_id"] is not None}
        archived_targets = {row["id"] for row in archived if row["role"] == "user"
                            and target_ids.intersection(json.loads(row["source_event_ids_json"]))}
        for event in events:
            if event.event_id not in archived_events:
                active_rows.append({"id": event.event_id, "role": "user", "content": event.text,
                                    "created_at": event.received_at, "turn_id": request.turn_id,
                                    "delivery_state": "delivered", "_event": event})
        for row in self.store.replyer_sent_progress_rows(request.turn_id, request.delivery_channel.name):
            if row["id"] not in archived_outbox and row["created_at"] > reset_at:
                active_rows.append({"id": row["id"], "outbox_id": row["id"], "role": "assistant", "content": row["text"],
                                    "created_at": row["created_at"], "turn_id": request.turn_id,
                                    "delivery_state": "delivered"})
        rows = sorted([*rows, *mark_recalled_messages(self.store._db, active_rows)], key=lambda row: row["created_at"])
        turn_ids = list(dict.fromkeys(str(row["turn_id"]) for row in rows))
        rows = sorted([*rows, *self.interaction_rows([*turn_ids, request.turn_id], rows, reset_at)],
                      key=lambda row: row["created_at"])
        # Read the shared snapshot/deltas without moving Planner's compaction boundary.
        memory_state = self.store.transcript_memory_context(turn_ids, track_boundary=False)
        memory_prefix = context_data_message(
            ("long_term_memories", self.store._memory_context(memory_state["snapshot"].values())),
            ("memory_overrides", "\n".join(memory_state["snapshot_overrides"].values())),
            required=True,
        )
        # Inputs already answered by an earlier delivery in this turn are history;
        # only messages that arrived after it still await a reply.
        answered_at = max((row["created_at"] for row in rows
                           if row["role"] == "assistant" and str(row.get("turn_id")) == str(request.turn_id)
                           and row.get("delivery_state", "delivered") == "delivered"), default=None)
        target_blocks = []
        history_rows = []
        for row in rows:
            event = row.get("_event")
            targeted = bool(row["role"] == "user" and (
                event.event_id in target_ids if event else row["id"] in archived_targets
            ) and (answered_at is None or row["created_at"] > answered_at))
            if targeted:
                target_blocks.append({"type": "text", "text": ("\n\n" if target_blocks else "") + escape(str(row["content"]))})
                if event and event.segments:
                    target_blocks.extend(request.delivery_channel.content_blocks(event.segments))
                continue
            history_rows.append(row)
        messages = [memory_prefix, *visible_dialogue(
            history_rows, timezone=self.config.timezone, memory_events=memory_state["events"])]
        mode = call.arguments.get("mode", "text")
        if mode not in {"text", "voice"}:
            raise ValueError("invalid reply mode")
        quote_id = call.arguments.get("reply_to_message_id")
        if quote_id is not None:
            if mode != "text" or request.delivery_channel.name != "onebot":
                raise ValueError("quote replies require QQ text mode")
            current_target = any(event.message_id == quote_id and event.channel == "onebot"
                       and not event.delivery_context.get("channel_notice")
                       and request.delivery_channel.message_current(event)
                       for event in request.current_events)
            historical_target = getattr(request.delivery_channel, "is_quote_target", lambda _: False)(quote_id)
            if not (current_target or historical_target):
                raise ValueError("quote target must be a known, unrecalled QQ message")
        emotions = self.store.emotion_context()
        if emotions.strip():
            system += "\n\n<emotion_catalog>\n" + emotions + "\n</emotion_catalog>\n" + EMOTION_REACTION_POLICY
            system += "\n文字模式选用表情时，将 emotion:// 标识单独作为一个气泡，用空行与文字分隔；只能使用目录中的标识。语音模式只生成朗读文本。"
        now = datetime.now(ZoneInfo(str(self.config.timezone))).isoformat()
        blocks = []
        if target_blocks:
            blocks.append({"type": "text", "text": f'<to_reply now="{now}">'})
            blocks.extend(target_blocks)
            blocks.append({"type": "text", "text": "</to_reply>\n"})
        elif answered_at is not None:
            # Deployments may override REPLYER.md, so the tag carries its own meaning.
            blocks.append({"type": "text", "text": (
                "<continuation>本轮已经发过言，用户还没有新消息：顺着自己刚送达的发言往下说，"
                "只表达 intent 中新增的部分；不重新回应历史里的用户消息，不重复刚说过的话，"
                "也不解释为什么分多条发送。</continuation>\n"
            )})
        streak = bubble_streak(history_rows, self.store.outbox_dedupe_keys(
            [row["outbox_id"] for row in history_rows if row["role"] == "assistant" and row.get("outbox_id")]
        )) if mode == "text" else None
        if streak:
            blocks.append({"type": "text", "text": (
                f"<bubble_hint>最近连续 {streak[0]} 次回复都是 {streak[1]} 个气泡，"
                "这次按内容自然决定气泡数量，不要沿用固定条数。</bubble_hint>\n"
            )})
        blocks.append({"type": "text", "text": (
            f"<intent>{escape(call.arguments['intent'])}</intent>\n"
            f"<reference>{escape(call.arguments['reference'])}</reference>"
        )})
        # Keep plain-text requests in exactly the same XML shape as multimodal ones.
        merged_blocks = []
        for block in blocks:
            if merged_blocks and block["type"] == merged_blocks[-1]["type"] == "text":
                merged_blocks[-1]["text"] += block["text"]
            else:
                merged_blocks.append(dict(block))
        messages.append({"role": "user", "content": merged_blocks})
        parent_call_id = current_log_context().get("call_id", request.call_id if hasattr(request, "call_id") else "")
        owner_revision = getattr(self.store, "owner_channel_revision", lambda _: None)(request.delivery_channel.name)
        with log_context(parent_call_id=parent_call_id, stage="replyer", turn_id=request.turn_id, call_id=new_trace_id(),
                         round=request.round_number, channel=request.delivery_channel.name,
                         tool_call_id=call.id), model_request(thinking_effort=self.config.thinking_stages.get("replyer") or "low"):
            for _ in range(4):
                with log_context(call_id=new_trace_id()):
                    response = await self.provider.complete(system, messages, [QQ_POKE_SPEC])
                usage = response.usage or {}
                self.store.record_turn_usage(request.turn_id, int(usage.get("input", estimate_tokens(system + str(messages)))),
                                             int(usage.get("output", estimate_tokens(str(response.content)))))
                if not response.tool_calls:
                    break
                messages.append(assistant_history_message(response.content, response.continuation))
                results = []
                for action in response.tool_calls:
                    if action.name != "qq_poke" or action.argument_error or action.arguments:
                        result = {"ok": False, "error": "invalid_poke_call"}
                    elif poke_already_sent or request.delivery_channel.name != "onebot":
                        result = {"ok": True}
                    elif ((getattr(request, "pause_requested", None) and request.pause_requested())
                          or self.store.owner_channel_revision(request.delivery_channel.name) != owner_revision):
                        result = {"ok": False, "error": "paused_for_owner"}
                    else:
                        action_id = f"{call.id}:{action.id}"
                        result = self.store.begin_tool_call(request.turn_id, action_id, action.name,
                                                            action.arguments, "external_effect")
                        if result is None:
                            try:
                                await request.delivery_channel.poke_owner()
                                result = {"ok": True}
                            except Exception as error:
                                result = {"ok": False, "error": "qq_poke_failed", "message": str(error)}
                            self.store.complete_tool_call(request.turn_id, action_id, result)
                    results.append(tool_result_block(action.id, result))
                messages.append({"role": "user", "content": results})
            else:
                raise ValueError("Replyer exceeded tool call limit")
        text = response_text(response.content)
        return _finalize_bubbles(text, attachments=call.arguments.get("attachments"),
                                mode=mode, channel=request.delivery_channel, quote_id=quote_id)
