from typing import Any

from ...storage import MEMORY_KINDS


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
                    "Durable claims worth retaining beyond today. May be empty; do not "
                    "manufacture lessons to fill the list. Do not record a specific or "
                    "shared experience/event; those belong to the Episode summary."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": sorted(MEMORY_KINDS),
                            "description": (
                                "profile: who the owner is, their background, rhythm, "
                                "and habits; preference: what the owner wants, including "
                                "constraints and standing wording; relationship: the bond "
                                "and its boundaries, forms of address, agreements; "
                                "third_party: stable facts about other people; practice: "
                                "reusable methods or decision processes, including tool "
                                "usage; world_knowledge: observed knowledge about the world; "
                                "self_insight: subjective understanding of your own feelings "
                                "or tendencies; cross_event_state: a durable state that "
                                "outlives the event that produced it. A specific or shared "
                                "experience belongs to the day's Episode, not here; record "
                                "only a durable claim matching the selected kind."
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
                                "For practice, include applicability and an "
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
