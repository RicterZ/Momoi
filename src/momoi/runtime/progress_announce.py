import copy
from typing import Any


ANNOUNCE_BUILTINS = frozenset({"curl"})
ANNOUNCE_FIELD = "say_to_owner"
ANNOUNCE_MARKER = "Delivered on the primary channel before this tool runs."


def should_announce(name: str, *, mcp: bool) -> bool:
    return mcp or name in ANNOUNCE_BUILTINS


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
            "A complete natural-language notice to the owner, in Momoi's "
            "voice, about what is about to happen. Write a finished spoken "
            "sentence, not a status label or heading, and do not end with a "
            f"colon. {ANNOUNCE_MARKER} Do not mention tool names or "
            "internals, and do not also send_message just to announce the "
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
