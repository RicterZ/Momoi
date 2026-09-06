from typing import Any

from ....storage.memory_values import MEMORY_ACTIVATIONS, MEMORY_KINDS

_EVIDENCE = {
    "type": "array",
    "minItems": 1,
    "items": {
        "type": "object",
        "properties": {
            "event_id": {"type": "string"},
            "quote": {"type": "string", "minLength": 1},
        },
        "required": ["event_id", "quote"],
        "additionalProperties": False,
    },
}
_MEMORY = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": sorted(MEMORY_KINDS)},
        "key": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_.-]{0,199}$"},
        "content": {"type": "string", "minLength": 1, "maxLength": 2000},
        "activation": {"type": "string", "enum": sorted(MEMORY_ACTIVATIONS)},
        "expires_at": {
            "type": ["number", "null"],
            "description": "Absolute Unix expiry from owner evidence for recent; null for recall/always. Never extend a past deadline.",
        },
    },
    "required": ["kind", "key", "content", "activation", "expires_at"],
    "additionalProperties": False,
}
MEMORY_OPERATION_FINISH_SPEC: dict[str, Any] = {
    "name": "memory_operation_finish",
    "description": "Terminal action, called alone. Resolve every operation exactly once; atomically apply the complete decision batch. Omitted current memories remain unchanged.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "minItems": 1,
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
