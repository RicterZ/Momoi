import json
import logging
from collections.abc import Callable, Sequence
from importlib.resources import files
from typing import Any
from xml.sax.saxutils import escape

from ..context_time import context_timestamp
from ..logging_context import log_event, safe_preview
from ..models import TurnDraft
from ..provider import ProviderError
from .budget import TOOL_RESULT_FITTER

logger = logging.getLogger(__name__)
PROMPT_ROOT = files("momoi").joinpath("prompts")
SYSTEM_PROMPT_PATH = PROMPT_ROOT.joinpath("system.md")
STYLE_CARD_PROMPT_PATH = PROMPT_ROOT.joinpath("style_card.md")
WEBHOOK_PROMPT_PATH = PROMPT_ROOT.joinpath("webhook.md")
HEARTBEAT_PROMPT_PATH = PROMPT_ROOT.joinpath("heartbeat.md")
HEARTBEAT_PLANNER_PROMPT_PATH = PROMPT_ROOT.joinpath("heartbeat_planner.md")
REPLY_WAIT_PROMPT_PATH = PROMPT_ROOT.joinpath("reply_wait.md")
REFLECTION_PROMPT_PATH = PROMPT_ROOT.joinpath("reflection.md")
MEMORY_MAINTENANCE_PROMPT_PATH = PROMPT_ROOT.joinpath(
    "memory_maintenance.md"
)
EPISODE_SUMMARY_PROMPT_PATH = PROMPT_ROOT.joinpath("episode_summary.md")
EPISODE_CONSOLIDATION_PROMPT_PATH = PROMPT_ROOT.joinpath(
    "episode_consolidation.md"
)
STYLE_CARD_SYSTEM_PROMPT = STYLE_CARD_PROMPT_PATH.read_text(encoding="utf-8").strip()
WEBHOOK_SYSTEM_PROMPT = WEBHOOK_PROMPT_PATH.read_text(encoding="utf-8").strip()
HEARTBEAT_SYSTEM_PROMPT = HEARTBEAT_PROMPT_PATH.read_text(encoding="utf-8").strip()
HEARTBEAT_PLANNER_SYSTEM_PROMPT = HEARTBEAT_PLANNER_PROMPT_PATH.read_text(
    encoding="utf-8"
).strip()
REPLY_WAIT_SYSTEM_PROMPT = REPLY_WAIT_PROMPT_PATH.read_text(encoding="utf-8").strip()
REFLECTION_SYSTEM_PROMPT = REFLECTION_PROMPT_PATH.read_text(encoding="utf-8").strip()
MEMORY_MAINTENANCE_SYSTEM_PROMPT = MEMORY_MAINTENANCE_PROMPT_PATH.read_text(
    encoding="utf-8"
).strip()
EPISODE_SUMMARY_SYSTEM_PROMPT = EPISODE_SUMMARY_PROMPT_PATH.read_text(
    encoding="utf-8"
).strip()
EPISODE_CONSOLIDATION_SYSTEM_PROMPT = (
    EPISODE_CONSOLIDATION_PROMPT_PATH.read_text(encoding="utf-8").strip()
)
MAX_CONSECUTIVE_TOOL_FAILURES = 3
AGENDA_POLICY_TOOLS = frozenset(
    {
        "goal_create",
        "goal_update",
        "goal_finish",
        "goal_cancel",
        "owner_notify",
    }
)
MEMORY_POLICY_TOOLS = frozenset({"memory_remember", "memory_forget"})
THINKING_POLICY_TOOLS = frozenset({"thinking_search", "thinking_read"})


def live_prompt(path: Any, fallback: str, *, optional: bool = False) -> str:
    try:
        if optional and not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        log_event(
            logger,
            logging.WARNING,
            "prompt_reload_failed",
            path=str(path),
            error_type=type(error).__name__,
            reason=safe_preview(str(error), 300),
        )
        return "" if optional else fallback
    return text if text or optional else fallback


def sections(*items: tuple[str, str]) -> str:
    return "\n\n".join(
        f"<{name}>\n{escape(value.strip())}\n</{name}>"
        for name, value in items
        if value.strip()
    )


# Prefix-cache order for DeepSeek/OpenAI (byte 0 of the user text). Durable
# owner memory and the bounded recent-memory baseline are always available
# first, followed by the fixed agenda state. Query-specific recall and
# conversation evidence come after that stable semantic prefix.
USER_CONTEXT_SECTION_ORDER = (
    "long_term_memories",
    "recent_memories",
    "goal_directory",
    "goal_progress",
    "active_goals",
    "interrupted_reply_expectation",
    "recent_external_events",
    "candidate_episodes",
    "recent_recall_context",
    "recall_memories",
    "recall_status",
    "reflection_memories",
    "episode_directory",
    "webhook_activity",
    "open_conversations",
    "recent_topic_reference",
    "recent_heartbeat_activities",
    "heartbeat_plan",
    "runtime_directives",
    "conversation_state",
    "runtime_state",
    "workflow_contract",
    "followup",
    "due_goal",
    "reflection_scope",
    "mood_timeline",
    "topic_timeline",
    "mutation_timeline",
    "tool_timeline",
    "current_webhook_task",
    "daily_reflection_record",
    "autonomous_heartbeat",
    "current_owner_messages",
)

# The two ordering rules sit in the last position because the live endpoint
# follows them reliably there. Field semantics remain in the tool schemas.
OWNER_TURN_PROTOCOL_REMINDER = (
    "[Trusted runtime Owner Turn protocol. "
    "1. Call recall first, before any other action. Every independent intent "
    "must search or reuse; there is no skip. The harness rejects another first "
    "action. "
    "2. Every response in this Turn must consist only of tool calls; ordinary "
    "assistant text is discarded. "
    "3. Every owner-visible bubble MUST be sent by calling send_message with "
    "that bubble in messages. Never output the bubble as ordinary assistant "
    "content.]"
)


def pack_user_context(*items: tuple[str, str]) -> str:
    """Render user context sections in prefix-cache order, skipping empties."""
    unknown = [name for name, _ in items if name not in USER_CONTEXT_SECTION_ORDER]
    if unknown:
        raise ValueError(f"unknown user context section: {unknown[0]}")
    by_name = {name: value for name, value in items}
    return sections(
        *(
            (name, by_name[name])
            for name in USER_CONTEXT_SECTION_ORDER
            if name in by_name
        )
    )


def context_data_message(
    *items: tuple[str, str], required: bool = False
) -> dict[str, Any] | None:
    """Carry non-dialogue data ahead of the conversation it applies to.

    Durable memory and Goal identities barely change between Turns, but a prefix
    cache only survives up to the first byte that differs, so anything placed
    after the transcript is reprocessed every Turn. Sitting before it, this
    material stays cached. It remains in the data region rather than the system
    contract because memory is written from conversation and tool observations,
    and giving that text instruction authority is exactly what the contract
    forbids.
    """

    text = pack_user_context(*items)
    if not text and required:
        text = (
            "<runtime_directives>\n"
            "The following native messages are shared conversation evidence.\n"
            "</runtime_directives>"
        )
    if not text:
        return None
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": text,
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }


def owner_context_message(*items: tuple[str, str]) -> dict[str, Any] | None:
    """Carry slow-changing Owner context before its native transcript."""

    return context_data_message(*items)


def owner_content_blocks(
    events: Sequence[Any],
    content_blocks: Callable[[Any], list[dict[str, Any]]],
    runtime_text: str = "",
) -> list[dict[str, Any]]:
    """Lay out the current owner input with each attachment beside its words.

    Collecting every attachment at the end leaves the model unable to tell which
    message an image belongs to, which matters as soon as the owner sends a
    picture between two sentences. Text blocks concatenate on the wire, so the
    section tags still enclose the whole input exactly as before.
    """

    blocks: list[dict[str, Any]] = []
    if runtime_text:
        # Blocks are concatenated without a separator on the wire, so the gap
        # that keeps the sections readable has to be part of the text.
        blocks.append({"type": "text", "text": f"{runtime_text}\n\n"})
    for index, event in enumerate(events):
        line = f"{context_timestamp(event.occurred_at)} {event.text}".strip()
        opening = "<current_owner_messages>\n" if index == 0 else ""
        blocks.append({"type": "text", "text": f"{opening}{escape(line)}"})
        blocks.extend(content_blocks(event.segments))
    closing = "</current_owner_messages>" if events else ""
    blocks.append(
        {
            "type": "text",
            "text": f"{closing}\n\n{OWNER_TURN_PROTOCOL_REMINDER}".lstrip(),
        }
    )
    return blocks


def tool_result_block(call_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": call_id,
        "content": json.dumps(result, ensure_ascii=False),
        "is_error": not bool(result.get("ok")),
    }


def tool_error_block(call_id: str, error: object) -> dict[str, Any]:
    return tool_result_block(call_id, {"ok": False, "error": error})


def truncate_tool_result_json(value: str, limit: int) -> str:
    return TOOL_RESULT_FITTER.fit(value, limit)


def turn_tool_names(draft: TurnDraft) -> list[str]:
    return list(
        dict.fromkeys(
            str(item.get("tool") or "")
            for item in draft.tool_calls
            if item.get("tool")
        )
    )


def reconciliation_message(turn_id: str) -> str:
    short_id = turn_id[:12]
    return (
        "An external tool may have already run before this turn was interrupted. "
        "To avoid repeating the action, I did not continue automatically. "
        f"After checking the actual result, send /resolve {short_id} <result>, "
        f"or /resume {short_id} <current state> to continue."
    )


def provider_failure_message(error: ProviderError) -> str:
    detail = " ".join(str(error).split()) or type(error).__name__
    if len(detail) > 300:
        detail = detail[:297].rstrip() + "..."
    return f"The model service failed during this turn. Reason: {detail}"


class ExternalToolTurnError(RuntimeError):
    pass


class TurnBudgetExceeded(RuntimeError):
    pass


class OwnerMessagesChanged(RuntimeError):
    def __init__(self, updates: list[object]) -> None:
        super().__init__("owner_messages_changed")
        self.updates = updates
