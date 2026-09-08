from typing import Any

from ...storage.reflection_values import REFLECTION_MEMORY_KINDS


REFLECTION_FINISH_SPEC: dict[str, Any] = {
    "name": "reflection_finish",
    "description": (
        "Store the daily reflection, reusable learning, and conversation closures, "
        "then end this private Turn."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": 6000,
                "description": (
                    "Chinese diary of meaningful experiences, feelings, opinions, "
                    "changed understanding, and unresolved questions."
                ),
            },
            "conversation_actions": {
                "type": "array",
                "maxItems": 32,
                "description": (
                    "Housekeeping of <open_conversations>; empty when none is needed."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "episode_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "description": "Episode id from <open_conversations>.",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["close"],
                        },
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 400,
                        },
                    },
                    "required": ["episode_id", "action", "reason"],
                    "additionalProperties": False,
                },
            },
            "memories": {
                "type": "array",
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": sorted(REFLECTION_MEMORY_KINDS),
                            "description": (
                                "Use tool_skill for reusable knowledge about a "
                                "specific tool or integration. Use practice for a "
                                "reusable method, workflow, or decision process whose "
                                "main lesson is not a particular tool invocation."
                            ),
                        },
                        "key": {
                            "type": "string",
                            "description": "Stable lowercase dot-separated key.",
                        },
                        "content": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Concise reusable lesson, including when it applies "
                                "and how to verify success or avoid a known failure."
                            ),
                        },
                        "evidence": {
                            "type": "string",
                            "description": (
                                "Exact contiguous quote from the supplied day or tool "
                                "evidence."
                            ),
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                    },
                    "required": [
                        "kind",
                        "key",
                        "content",
                        "evidence",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["summary", "memories"],
        "additionalProperties": False,
    },
}
