from typing import Any


def tool_enable_spec(group_descriptions: dict[str, str]) -> dict[str, Any]:
    groups = {
        group: str(description).strip()
        for group, description in sorted(group_descriptions.items())
    }
    return {
        "name": "tool_enable",
        "description": (
            "Enable only the MCP groups needed for the next action. Groups: "
            + "; ".join(
                f"{group}: {description}" for group, description in groups.items()
            )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "groups": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": max(1, len(groups)),
                    "uniqueItems": True,
                    "items": {"type": "string", "enum": list(groups)},
                }
            },
            "required": ["groups"],
            "additionalProperties": False,
        },
    }


AUTONOMOUS_FINISH_SPEC: dict[str, Any] = {
    "name": "autonomous_finish",
    "description": (
        "Ends a Goal review without sending a notification; use send_bubbles to notify. "
        "Call it alone after updating, "
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
        "Continue a truncated tool-result snapshot without rerunning the tool. Pass "
        "result_ref unchanged and latest next_cursor; omit cursor for the first chunk. "
        "Cannot read workspace files."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "result_ref": {
                "type": "string",
                "pattern": "^tr_[0-9a-f]{32}$",
                "description": "result_ref from the truncated result.",
            },
            "cursor": {
                "type": "string",
                "minLength": 1,
                "description": "next_cursor from the preceding chunk.",
            },
        },
        "required": ["result_ref"],
        "additionalProperties": False,
    },
}
