from typing import Any

from ...storage.reflection_values import REFLECTION_MEMORY_KINDS


REFLECTION_FINISH_SPEC: dict[str, Any] = {
    "name": "reflection_finish",
    "description": (
        "Store the daily reflection, lasting memories, and conversation closures, "
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
                "description": (
                    "Experiences, understandings, or knowledge worth retaining beyond "
                    "today. May be empty; do not manufacture lessons to fill the list."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": sorted(REFLECTION_MEMORY_KINDS),
                            "description": (
                                "shared_experience: meaningful experiences together; "
                                "relationship: situated understanding of the relationship; "
                                "self_insight: subjective understanding of your own feelings "
                                "or tendencies; owner_profile/owner_preference: owner-stated "
                                "facts or preferences; world_knowledge: observed knowledge; "
                                "practice: reusable methods or decision processes; "
                                "tool_skill: knowledge of a specific tool or integration."
                            ),
                        },
                        "key": {
                            "type": "string",
                            "description": "Stable lowercase dot-separated key.",
                        },
                        "content": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1000,
                            "description": (
                                "Concisely describe what happened, was understood, or was "
                                "learned, according to kind, within the evidence's scope. "
                                "For practice/tool_skill, include applicability and an "
                                "observable outcome; absence of criticism is not evidence "
                                "of success."
                            ),
                        },
                        "evidence": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                            "description": (
                                "Exact contiguous quote from the supplied day or tool "
                                "evidence supporting this conclusion, not merely its topic."
                            ),
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": (
                                "Confidence that the evidence supports the recorded claim, "
                                "including its scope; not its importance or your resolve."
                            ),
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
