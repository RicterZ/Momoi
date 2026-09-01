from typing import Any


_TURN_IDS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "uniqueItems": True,
    "items": {"type": "string", "minLength": 1},
}
_TAGS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
}
_DEFER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["defer"]},
        "turn_ids": _TURN_IDS_SCHEMA,
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "required": ["action", "turn_ids", "reason"],
    "additionalProperties": False,
}
_IGNORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["ignore"]},
        "turn_ids": _TURN_IDS_SCHEMA,
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "required": ["action", "turn_ids", "reason"],
    "additionalProperties": False,
}
_CONTINUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["continue"]},
        "episode_id": {"type": "string", "minLength": 1},
        "turn_ids": _TURN_IDS_SCHEMA,
        "topics": _TAGS_SCHEMA,
        "entities": _TAGS_SCHEMA,
        "open_loops": _TAGS_SCHEMA,
        "salience": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "action",
        "episode_id",
        "turn_ids",
        "topics",
        "entities",
        "open_loops",
        "salience",
    ],
    "additionalProperties": False,
}
_NEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["new"]},
        "key": {
            "type": "string",
            "pattern": "^[a-z0-9][a-z0-9_-]{0,39}$",
        },
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "turn_ids": _TURN_IDS_SCHEMA,
        "topics": _TAGS_SCHEMA,
        "entities": _TAGS_SCHEMA,
        "open_loops": _TAGS_SCHEMA,
        "salience": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "action",
        "key",
        "title",
        "turn_ids",
        "topics",
        "entities",
        "open_loops",
        "salience",
    ],
    "additionalProperties": False,
}

EPISODE_CLASSIFY_TURNS_SPEC: dict[str, Any] = {
    "name": "episode_classify_turns",
    "description": (
        "Persist classifications for any non-empty, not-yet-covered subset of the "
        "pending Turns. Calls in one response must use non-overlapping Turn subsets. "
        "The result reports durable covered and remaining Turn ids."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "oneOf": [
                        _DEFER_SCHEMA,
                        _IGNORE_SCHEMA,
                        _CONTINUE_SCHEMA,
                        _NEW_SCHEMA,
                    ]
                },
            }
        },
        "required": ["decisions"],
        "additionalProperties": False,
    },
}

EPISODE_CONSOLIDATION_FINISH_SPEC: dict[str, Any] = {
    "name": "episode_consolidation_finish",
    "description": (
        "Finish classification only after every pending Turn has a durable Store "
        "decision. The runtime rejects this call while any Turn remains."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

_SUMMARY_CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message_id": {"type": "integer", "minimum": 1},
        "turn_id": {"type": "string", "minLength": 1},
        "ordinal": {"type": "integer", "minimum": 1},
        "quote": {"type": "string", "minLength": 1, "maxLength": 1000},
    },
    "required": ["message_id", "turn_id", "ordinal", "quote"],
    "additionalProperties": False,
}

EPISODE_SUMMARY_FINISH_SPEC: dict[str, Any] = {
    "name": "episode_summary_finish",
    "description": (
        "Submit the complete evidence-backed working summary for the claimed Episode. "
        "The runtime verifies every citation against archived raw messages before "
        "committing it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "minItems": 1,
                "maxItems": 64,
                "items": _SUMMARY_CLAIM_SCHEMA,
            },
            "narrative_summary": {"type": "string", "maxLength": 800},
            "emotional_context": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "maxLength": 300},
                    "momoi": {"type": "string", "maxLength": 300},
                    "tone": {"type": "string", "maxLength": 300},
                },
                "required": ["owner", "momoi", "tone"],
                "additionalProperties": False,
            },
            "outcomes": {
                "type": "array",
                "maxItems": 12,
                "items": {"type": "string", "minLength": 1, "maxLength": 500},
            },
        },
        "required": [
            "claims",
            "narrative_summary",
            "emotional_context",
            "outcomes",
        ],
        "additionalProperties": False,
    },
}
