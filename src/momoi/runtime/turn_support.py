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
CONTEXT_PLANNER_PROMPT_PATH = PROMPT_ROOT.joinpath("context_planner.md")
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
CONTEXT_PLANNER_PROTOCOL_PROMPT = CONTEXT_PLANNER_PROMPT_PATH.read_text(
    encoding="utf-8"
).strip()
DOWNSTREAM_OWNER_CONTRACT_PROMPT = SYSTEM_PROMPT_PATH.read_text(
    encoding="utf-8"
).strip()
PLANNER_STYLE_CARD_BOUNDARY = (
    "[Owner-only Style Card omitted. The downstream Owner applies its wording, "
    "tone, bubble shape, timing, and reaction guidance independently.]"
)
PLANNER_DOWNSTREAM_OWNER_CONTRACT_PROMPT = (
    DOWNSTREAM_OWNER_CONTRACT_PROMPT.replace(
        "{{STYLE_CARD}}",
        PLANNER_STYLE_CARD_BOUNDARY,
    )
)
CONTEXT_PLANNER_SYSTEM_PROMPT = (
    CONTEXT_PLANNER_PROTOCOL_PROMPT
    + "\n\n# Downstream Owner contract\n\n"
    + "The following is the exact system contract for the downstream Owner "
    + "model. It is a trusted planning constraint, not your identity, tool "
    + "protocol, or permission to act. Interpret its second-person commands as "
    + "requirements on the downstream Owner. Its Style Card body is omitted; "
    + "wording, tone, bubble, timing, and reaction guidance is outside planning. "
    + "`{{SOUL}}` remains unresolved; do not infer identity, relationships, "
    + "persona, or persona-specific wording from it.\n\n"
    + "<downstream_owner_contract>\n"
    + PLANNER_DOWNSTREAM_OWNER_CONTRACT_PROMPT
    + "\n</downstream_owner_contract>\n\n"
    + "# Planner boundary reminder\n\nThe downstream contract ends above. "
    + "Continue to follow the Context planning protocol: do not answer, send "
    + "messages, or execute work; submit exactly one `submit_context_plan` call."
)
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
    "active_goals",
    "always_memory_inventory",
    "recent_memory_inventory",
    "interrupted_reply_expectation",
    "recent_turn_base",
    "recent_turn_append",
    "recent_external_events",
    "recent_turns",
    "recent_conversation",
    "recall_memories",
    "recall_status",
    "reflection_memories",
    "episode_directory",
    "webhook_activity",
    "open_conversations",
    "recent_topic_reference",
    "recent_heartbeat_activities",
    "heartbeat_plan",
    "context_resolution",
    "runtime_directives",
    "conversation_state",
    "runtime_state",
    "reply_timeline",
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

OWNER_TURN_PROTOCOL_REMINDER = (
    "[Trusted runtime Owner Turn delivery rule: every owner-visible bubble MUST "
    "be sent by calling send_message with that bubble in messages. Never output "
    "the bubble as ordinary assistant content.]"
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


def pack_owner_context(*items: tuple[str, str]) -> str:
    """Render an Owner request with the fixed output protocol at the tail."""
    context = pack_user_context(*items)
    return f"{context}\n\n{OWNER_TURN_PROTOCOL_REMINDER}"


def owner_content_blocks(
    events: Sequence[Any], content_blocks: Callable[[Any], list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Lay out the current owner input with each attachment beside its words.

    Collecting every attachment at the end leaves the model unable to tell which
    message an image belongs to, which matters as soon as the owner sends a
    picture between two sentences. Text blocks concatenate on the wire, so the
    section tags still enclose the whole input exactly as before.
    """

    blocks: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        line = f"{context_timestamp(event.occurred_at)} {event.text}".strip()
        opening = "<current_owner_messages>\n" if index == 0 else ""
        blocks.append({"type": "text", "text": f"{opening}{escape(line)}"})
        blocks.extend(content_blocks(event.segments))
    closing = "</current_owner_messages>" if blocks else ""
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


def conversation_guidance(plan: dict[str, object]) -> str:
    intent_units = [
        unit for unit in plan.get("intent_units", []) if isinstance(unit, dict)
    ]
    uncertainty = plan.get("uncertainty", [])
    owner_handoff = plan.get("owner_handoff")
    if not intent_units and not uncertainty and not owner_handoff:
        return ""
    lines: list[str] = []
    for index, unit in enumerate(intent_units, start=1):
        lines.append(f"Owner intent {index}")
        lines.append(f"  owner text: {' '.join(str(unit.get('text') or '').split())}")
        lines.append(f"  intent: {' '.join(str(unit.get('intent') or '').split())}")
        lines.append(f"  speech act: {unit.get('speech_act') or 'unknown'}")
        for reference in unit.get("references") or []:
            lines.append(f"  reference: {' '.join(str(reference).split())}")

    if isinstance(owner_handoff, dict):
        context = owner_handoff.get("context")
        if isinstance(context, dict):
            needs = context.get("needs") or []
            if needs:
                lines.append("Possible post-recall context needs")
            for need in needs:
                if not isinstance(need, dict):
                    continue
                fields = " ".join(
                    f"{key}={' '.join(str(need.get(key) or '').split())}"
                    for key in ("tool", "query", "evidence")
                    if need.get(key) not in (None, "", [], {})
                )
                lines.append(f"  need: {fields}")

        mcp = owner_handoff.get("mcp")
        if isinstance(mcp, dict):
            servers = mcp.get("servers") or []
            if servers:
                lines.append("Preloaded MCP groups")
                lines.append("  " + ", ".join(str(server) for server in servers))

        strategy = owner_handoff.get("strategy")
        if isinstance(strategy, dict):
            lines.append("Strategic handoff")
            response_mode = str(strategy.get("response_mode") or "")
            lines.append(f"  response mode: {response_mode or 'unspecified'}")
            for index, item in enumerate(strategy.get("plan") or [], start=1):
                lines.append(f"  step {index}: {' '.join(str(item).split())}")
            for criterion in strategy.get("completion_criteria") or []:
                lines.append(f"  complete when: {' '.join(str(criterion).split())}")
            if response_mode == "silent":
                lines.append("  terminal action: call end_turn alone")
            elif response_mode == "visible":
                lines.append(
                    "  delivery rule: every owner-visible bubble MUST be sent by "
                    "calling send_message with that bubble in messages; never "
                    "output the bubble as ordinary assistant content"
                )

    for item in uncertainty or []:
        lines.append(f"Uncertainty: {' '.join(str(item).split())}")
    return "\n".join(lines)


def plan_log_units(plan: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            "id": unit.get("id"),
            "act": unit.get("speech_act"),
            "text": unit.get("text"),
            "intent": unit.get("intent"),
            "references": unit.get("references", []),
            "recall_mode": (
                unit.get("recall", {}).get("mode")
                if isinstance(unit.get("recall"), dict)
                else None
            ),
            "recall_queries": unit.get("recall_queries", []),
            "recall_from_turn_id": unit.get("recall_from_turn_id", ""),
        }
        for unit in plan.get("intent_units", [])
        if isinstance(unit, dict)
    ]


def plan_log_episodes(plan: dict[str, object]) -> list[dict[str, object]]:
    actions = plan.get("episode_actions", [])
    return [
        {
            "action": action.get("action"),
            "episode": action.get("episode_ref", action.get("episode_id")),
            "units": action.get("unit_ids", []),
            **({"title": action.get("title")} if action.get("title") else {}),
        }
        for action in actions
        if isinstance(action, dict)
    ]


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
