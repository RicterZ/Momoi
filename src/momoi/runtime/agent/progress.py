import copy
from typing import Any

from ...contracts import OWNER_PROGRESS_BEFORE_FIRST_CALL, OWNER_PROGRESS_FIELD
from ...models import ToolCall

ANNOUNCE_FIELD = "say_to_owner"
ANNOUNCE_DELIVERY_NOTE = "Delivered on the primary channel before this tool runs."
OWNER_PROGRESS_HOOK_FIELD = "x-momoi-owner-progress-hook"


def requests_owner_progress(spec: dict[str, Any]) -> bool:
    return spec.get(OWNER_PROGRESS_FIELD) == OWNER_PROGRESS_BEFORE_FIRST_CALL


def public_tool_spec(spec: dict[str, Any]) -> dict[str, Any]:
    public = copy.deepcopy(spec)
    public.pop(OWNER_PROGRESS_FIELD, None)
    public.pop(OWNER_PROGRESS_HOOK_FIELD, None)
    return public


def announce_field(spec: dict[str, Any]) -> str | None:
    if spec.get(OWNER_PROGRESS_HOOK_FIELD) != ANNOUNCE_FIELD:
        return None
    properties = (spec.get("input_schema") or {}).get("properties") or {}
    if not isinstance(properties, dict):
        return None
    if ANNOUNCE_FIELD in properties:
        return ANNOUNCE_FIELD
    return None


def decorate_tool_spec(spec: dict[str, Any]) -> dict[str, Any]:
    decorated = public_tool_spec(spec)
    decorated[OWNER_PROGRESS_HOOK_FIELD] = ANNOUNCE_FIELD
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
            "Owner-visible sentence required on the first external-work tool unless "
            "send_bubbles already acknowledged it; assistant text never counts and "
            "later rounds may omit it. Give an evidence-backed reaction, result, "
            "progress, failure, or route change—not a tool caption, retry, recap, or "
            f"success promise. {ANNOUNCE_DELIVERY_NOTE} Do not duplicate it with "
            "send_bubbles."
        ),
    }
    return decorated


def initial_announce_error_message(field: str) -> str:
    return (
        f"Before the first external-work tool batch for this owner request, "
        f"include one natural owner-visible {field} on the first such tool, or "
        "send_bubbles before it. Do not caption the tool or promise success. "
        "Later tool rounds may omit the field and run silently."
    )


def missing_initial_work_announce(
    calls: list[ToolCall],
    request_tools: list[dict[str, Any]],
    *,
    owner_work_acknowledged: bool,
) -> tuple[str, str] | None:
    if owner_work_acknowledged:
        return None
    announce_fields = {
        str(spec.get("name") or ""): announce_field(spec) for spec in request_tools
    }
    for index, call in enumerate(calls):
        field = announce_fields.get(call.name)
        if not field:
            continue
        if any(
            earlier.name == "send_bubbles" and bool(earlier.arguments.get("bubbles"))
            for earlier in calls[:index]
        ):
            return None
        if str(call.arguments.get(field) or "").strip():
            return None
        return call.id, field
    return None


def take_announce_message(
    arguments: dict[str, Any], field: str
) -> str | None:
    raw = arguments.pop(field, None)
    text = str(raw or "").strip()
    if not text:
        return None
    return text


def apply_tool_announce(
    arguments: dict[str, Any],
    field: str | None,
) -> str | None:
    if not field:
        return None
    return take_announce_message(arguments, field)
