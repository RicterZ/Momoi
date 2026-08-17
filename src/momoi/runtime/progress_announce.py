import copy
from typing import Any


ANNOUNCE_LOCAL_TOOLS = frozenset(
    {
        "curl",
        "goal_create",
        "goal_cancel",
        "reminder_create",
        "reminder_cancel",
    }
)
ANNOUNCE_FIELD = "say_to_owner"
ANNOUNCE_MARKER = "Delivered on the primary channel before this tool runs."


def should_announce(name: str, *, mcp: bool) -> bool:
    return mcp or name in ANNOUNCE_LOCAL_TOOLS


def should_deliver_announce(
    *,
    heartbeat_turn: bool,
    reply_wait_turn: bool,
    autonomous_goal: bool = False,
) -> bool:
    return not heartbeat_turn and not reply_wait_turn and not autonomous_goal


def announce_field(spec: dict[str, Any]) -> str | None:
    properties = (spec.get("input_schema") or {}).get("properties") or {}
    if not isinstance(properties, dict):
        return None
    description = str((properties.get(ANNOUNCE_FIELD) or {}).get("description") or "")
    if ANNOUNCE_MARKER in description:
        return ANNOUNCE_FIELD
    return None


def decorate_tool_spec(spec: dict[str, Any]) -> dict[str, Any]:
    decorated = copy.deepcopy(spec)
    schema = decorated.setdefault("input_schema", {"type": "object"})
    if not isinstance(schema, dict):
        return spec
    properties = schema.setdefault("properties", {})
    if not isinstance(properties, dict):
        return spec
    properties[ANNOUNCE_FIELD] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 300,
        "description": (
            "Optional spoken bridge the owner will hear in the Soul's voice. "
            "On the first external-work batch after a new owner request, the "
            "first such tool must include this field unless send_message already "
            "acknowledged the work. Later tool rounds may omit it to run silently. "
            "This is a standalone reply to the owner, not the tool's caption; "
            "never narrate a retry or promise an unverified outcome. If this Turn "
            "has no tool result yet, accept and go without recapping the request. "
            "After a tool result, continue from what that result actually showed—a "
            "finding, failure, or changed route—and never reopen the original request. "
            f"A finished spoken sentence, not a colon-ended label. "
            f"{ANNOUNCE_MARKER} Do not also send_message for the same action."
        ),
    }
    return decorated


def initial_announce_error_message(field: str) -> str:
    return (
        f"Before the first external-work tool batch for this owner request, "
        f"include one natural owner-visible {field} on the first such tool, or "
        "send_message before it. Do not caption the tool or promise success. "
        "Later tool rounds may omit the field and run silently."
    )


def take_announce_message(
    arguments: dict[str, Any], field: str
) -> tuple[str | None, str | None]:
    raw = arguments.pop(field, None)
    text = str(raw or "").strip()
    if not text:
        return None, None
    return text, None


def apply_tool_announce(
    arguments: dict[str, Any],
    field: str | None,
    *,
    deliver: bool,
) -> tuple[str | None, str | None]:
    if not field:
        return None, None
    if not deliver:
        arguments.pop(field, None)
        return None, None
    return take_announce_message(arguments, field)
