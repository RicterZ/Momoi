from typing import Any

from ....storage.memory.memory_values import (
    ALWAYS_MEMORY_KINDS,
    MEMORY_ACTIVATIONS,
    MEMORY_KINDS,
)

_EVIDENCE = {
    "type": "array",
    "minItems": 1,
    "description": "Exact owner citations supporting the change and every resolved request.",
    "items": {
        "type": "object",
        "properties": {
            "event_id": {
                "type": "string",
                "description": "Supplied authenticated owner event ID.",
            },
            "quote": {
                "type": "string",
                "minLength": 1,
                "description": "Exact contiguous substring of that event.",
            },
        },
        "required": ["event_id", "quote"],
        "additionalProperties": False,
    },
}
_MEMORY = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string", "enum": sorted(MEMORY_KINDS),
            "description": (
                "Canonical subject kind: profile, preference, relationship, third_party, "
                "practice, world_knowledge, self_insight, or cross_event_state. "
                "A specific/shared experience is an Episode, never a memory."
            ),
        },
        "key": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_.-]{0,199}$"},
        "content": {"type": "string", "minLength": 1, "maxLength": 2000},
        "activation": {"type": "string", "enum": sorted(MEMORY_ACTIVATIONS)},
        "expires_at": {
            "type": "null",
            "description": "Memories do not expire; always null. Temporary state belongs to current state.",
        },
    },
    "required": ["kind", "key", "content", "activation", "expires_at"],
    "oneOf": [
        {
            "properties": {
                "activation": {"enum": ["recall"]},
            }
        },
        {
            "properties": {
                "activation": {"enum": ["always"]},
                "kind": {"enum": sorted(ALWAYS_MEMORY_KINDS)},
            }
        },
    ],
    "additionalProperties": False,
}
MEMORY_OPERATION_FINISH_SPEC: dict[str, Any] = {
    "name": "memory_operation_finish",
    "description": "Atomically apply the complete decision batch and end this private Turn. Call alone. Omitted current memories remain unchanged.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "minItems": 1,
                "description": "Resolve every supplied operation exactly once; combine requests concerning the same fact.",
                "items": {
                    "type": "object",
                    "properties": {
                        "operation_ids": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "string"},
                        },
                        "action": {
                            "type": "string",
                            "enum": ["write", "forget", "noop", "defer"],
                        },
                        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
                        "target_ids": {
                            "type": "array",
                            "description": "Current memory IDs to replace, merge, or forget; empty for a new independent fact.",
                            "uniqueItems": True,
                            "items": {"type": "integer", "minimum": 1},
                        },
                        "memory": _MEMORY,
                        "evidence": _EVIDENCE,
                    },
                    "required": ["operation_ids", "action", "reason"],
                    "oneOf": [
                        {
                            "properties": {"action": {"enum": ["write"]}},
                            "required": ["target_ids", "memory", "evidence"],
                        },
                        {
                            "properties": {
                                "action": {"enum": ["forget"]},
                                "target_ids": {"minItems": 1},
                                "memory": False,
                            },
                            "required": ["target_ids", "evidence"],
                        },
                        {
                            "properties": {
                                "action": {"enum": ["noop", "defer"]},
                                "target_ids": False,
                                "memory": False,
                                "evidence": False,
                            }
                        },
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["decisions"],
        "additionalProperties": False,
    },
}
MEMORY_OPERATION_SEARCH_SPEC = {
    "name": "memory_operation_search",
    "description": "Optional read-only lookup when supplied memories cannot identify a target or related duplicate. Searches active confirmed memories across activations. Results become eligible targets. Do not search merely to repeat supplied evidence.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 240,
                "description": (
                    "A concise literal subject or alternative phrases separated by |, "
                    "for example 面试|interview or OAuth2|cloud sandbox. Each alternative "
                    "is matched as a whole phrase by keyword search; spaces within a "
                    "phrase are preserved, not keyword separators. Do not concatenate "
                    "unrelated terms and dates into one query phrase."
                ),
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}
