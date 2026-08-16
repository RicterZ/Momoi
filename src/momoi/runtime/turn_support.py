import json
import logging
from importlib.resources import files
from typing import Any
from xml.sax.saxutils import escape

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
CONTEXT_PLANNER_SYSTEM_PROMPT = CONTEXT_PLANNER_PROMPT_PATH.read_text(
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
        "reminder_create",
        "reminder_cancel",
        "owner_notify",
    }
)
MEMORY_POLICY_TOOLS = frozenset({"memory_remember", "memory_forget"})


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
        {
            "owner_text": unit["text"],
            "speech_act": unit.get("speech_act", "unknown"),
            **({"references": unit["references"]} if unit.get("references") else {}),
        }
        for unit in plan.get("intent_units", [])
        if isinstance(unit, dict) and (unit.get("speech_act") or unit.get("references"))
    ]
    uncertainty = plan.get("uncertainty", [])
    if not intent_units and not uncertainty:
        return ""
    return json.dumps(
        {"owner_intent_units": intent_units, "uncertainty": uncertainty},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def plan_log_units(plan: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            "id": unit.get("id"),
            "act": unit.get("speech_act"),
            "text": unit.get("text"),
            "intent": unit.get("intent"),
            "references": unit.get("references", []),
            "queries": unit.get("recall_queries", []),
        }
        for unit in plan.get("intent_units", [])
        if isinstance(unit, dict)
    ]


def plan_log_episodes(plan: dict[str, object]) -> list[dict[str, object]]:
    actions = plan.get("episode_actions", plan.get("episode_bindings", []))
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
