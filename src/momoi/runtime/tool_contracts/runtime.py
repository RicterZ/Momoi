from typing import Any


def tool_enable_spec(group_descriptions: dict[str, str]) -> dict[str, Any]:
    groups = {
        group: str(description).strip()
        for group, description in sorted(group_descriptions.items())
    }
    return {
        "name": "tool_enable",
        "description": "Enable the MCP groups needed for the next action.",
        "input_schema": {
            "type": "object",
            "properties": {
                "groups": {
                    "type": "array",
                    "description": "; ".join(
                        f"{group}: {description}" for group, description in groups.items()
                    ),
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


READ_TOOL_RESULT_SPEC: dict[str, Any] = {
    "name": "read_tool_result",
    "description": (
        "Continue a truncated tool-result snapshot without rerunning the tool. "
        "Cannot read workspace files."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "result_ref": {
                "type": "string",
                "pattern": "^tr_[0-9a-f]{32}$",
                "description": "Copy result_ref unchanged from the truncated result.",
            },
            "cursor": {
                "type": "string",
                "minLength": 1,
                "description": "Latest next_cursor from the preceding chunk; omit for the first chunk.",
            },
        },
        "required": ["result_ref"],
        "additionalProperties": False,
    },
}
