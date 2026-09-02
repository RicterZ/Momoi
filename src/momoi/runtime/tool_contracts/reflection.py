from typing import Any

from ...storage import REFLECTION_MEMORY_KINDS


REFLECTION_FINISH_SPEC: dict[str, Any] = {
    "name": "reflection_finish",
    "description": (
        "Required terminal result for the private daily retrospective. It stores the "
        "reflection record, promotes only durable evidence-backed learning, and "
        "optionally closes finished conversations. Confirmed owner memory is read-only "
        "in Reflection and is maintained by a separate runtime stage."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": 6000,
                "description": (
                    "A grounded, thoughtful Chinese diary of the day, not a timeline. "
                    "Use the mood, topic, and mutation timelines together with the "
                    "day record. Select meaningful moments, connect causes and consequences, and "
                    "write Momoi's own feelings, opinions, changed understanding, and "
                    "unresolved questions in coherent, elegant prose. Do not invent "
                    "facts or expose hidden chain-of-thought."
                ),
            },
            "conversation_actions": {
                "type": "array",
                "maxItems": 32,
                "description": (
                    "Optional housekeeping of `<open_conversations>`. Empty is valid. "
                    "Use episode_id from that inventory only. Close a thread only when "
                    "it is finished, expired, or superseded."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "episode_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "description": "Open or closing conversation episode id.",
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

