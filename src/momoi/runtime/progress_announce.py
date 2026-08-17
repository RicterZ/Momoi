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
            "A short spoken line the owner will hear, in the Soul's voice. "
            "Choose it with the same reply logic as send_message: the owner's "
            "answer, not this tool's caption. Never narrate what this tool "
            "does. If this Turn has no tool result yet, answer the owner: "
            "accept and go, without recapping the request. After a tool "
            "result, continue from that result; do not answer the original "
            "request again. A finished spoken sentence, not a colon-ended "
            f"label. {ANNOUNCE_MARKER} Do not also send_message for the "
            "same action."
        ),
    }
    required = list(schema.get("required") or [])
    if ANNOUNCE_FIELD not in required:
        required.append(ANNOUNCE_FIELD)
    schema["required"] = required
    return decorated


def announce_error_message(field: str, error: str) -> str:
    return (
        f"This tool requires a short owner-visible {field} that naturally "
        "tells the owner what you are about to do. Call it again with that "
        "field set."
    )


def take_announce_message(
    arguments: dict[str, Any], field: str
) -> tuple[str | None, str | None]:
    raw = arguments.pop(field, None)
    text = str(raw or "").strip()
    if not text:
        return None, "say_to_owner_required"
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
