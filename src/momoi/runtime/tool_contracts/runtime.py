from typing import Any

from ...tools.builtin import BUILTIN_TOOL_SPECS


CURL_TOOL_SPEC = next(spec for spec in BUILTIN_TOOL_SPECS if spec["name"] == "curl")

def tool_enable_spec(group_descriptions: dict[str, str]) -> dict[str, Any]:
    ordered_groups = {
        group: str(description).strip()
        for group, description in sorted(group_descriptions.items())
    }
    group_ids = list(ordered_groups)
    catalog = "; ".join(
        f"{group}: {description}"
        for group, description in ordered_groups.items()
    )
    return {
        "name": "tool_enable",
        "description": (
            "Load omitted MCP tool groups when required. Loaded tools "
            "become callable on the next model step. "
            f"Group examples: {catalog}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "groups": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": max(1, len(group_ids)),
                    "items": {
                        "type": "string",
                        "enum": group_ids,
                    },
                }
            },
            "required": ["groups"],
            "additionalProperties": False,
        },
    }

AUTONOMOUS_FINISH_SPEC: dict[str, Any] = {
    "name": "autonomous_finish",
    "description": (
        "Required terminal marker for a Goal review. Call it alone after updating, "
        "finishing, or cancelling the Goal and after any optional send_bubbles call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

READ_TOOL_RESULT_SPEC: dict[str, Any] = {
    "name": "read_tool_result",
    "description": (
        "Continue reading an exact private snapshot when a tool result returned "
        "truncated=true, result_ref, and next_cursor. Pass result_ref unchanged "
        "and the latest next_cursor; omit cursor only for the first chunk. This "
        "does not call the original tool again and cannot read workspace files."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "result_ref": {
                "type": "string",
                "pattern": "^tr_[0-9a-f]{32}$",
                "description": "Opaque result_ref returned by a truncated tool result.",
            },
            "cursor": {
                "type": "string",
                "minLength": 1,
                "description": "Opaque next_cursor from the preceding chunk.",
            },
        },
        "required": ["result_ref"],
        "additionalProperties": False,
    },
}
