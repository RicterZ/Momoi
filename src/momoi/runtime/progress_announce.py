import copy
from dataclasses import dataclass
from typing import Any


ANNOUNCE_LOCAL_TOOLS = frozenset(
    {
        "curl",
        "goal_create",
        "goal_cancel",
    }
)
ANNOUNCE_FIELD = "say_to_owner"
ANNOUNCE_MARKER = "Delivered on the primary channel before this tool runs."


@dataclass(frozen=True)
class ProgressPolicy:
    """Whether a tool surface may request an owner-visible work acknowledgement."""

    external: bool = False
    local_work: bool = False


OWNER_PROGRESS_POLICY = ProgressPolicy(external=True, local_work=True)
SILENT_PROGRESS_POLICY = ProgressPolicy()


def should_announce(name: str, *, mcp: bool) -> bool:
    policy = OWNER_PROGRESS_POLICY if mcp else (
        ProgressPolicy(local_work=True) if name in ANNOUNCE_LOCAL_TOOLS
        else SILENT_PROGRESS_POLICY
    )
    return policy.external or policy.local_work


def should_deliver_announce(
    *,
    authority: str,
) -> bool:
    return authority == "owner"


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
            "Conditionally required owner-visible sentence before this tool. The "
            "first external-work batch after a new owner request MUST include it on "
            "the first tool unless send_bubbles already acknowledged the work. "
            "Ordinary assistant content is discarded and does not satisfy this "
            "requirement. Later tool rounds may omit it. Use only an evidence-backed "
            "reaction, result, "
            "progress, failure, or route change—not a tool caption, retry narration, "
            "request recap, or promise of success. "
            f"{ANNOUNCE_MARKER} Do not also send_bubbles for the same action."
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
