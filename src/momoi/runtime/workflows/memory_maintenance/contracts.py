MAINTENANCE_ACTIONS = {"replace", "merge", "retire"}
MEMORY_MAINTENANCE_RUN_VERSION = "v1"

_EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "event_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Copy event_id verbatim from <owner_evidence>, including its channel prefix."
            ),
        },
        "quote": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Exact contiguous substring of that event's content; no paraphrase."
            ),
        },
    },
    "required": ["event_id", "quote"],
    "additionalProperties": False,
}

MEMORY_MAINTENANCE_FINISH_SPEC: dict[str, object] = {
    "name": "memory_maintenance_finish",
    "description": (
        "Apply the confirmed-memory maintenance batch and end this private Turn."
    ),
    "input_schema": {
        "type": "object",
        "description": "Cover every mutable id exactly once: unchanged in reviewed_ids, changed, or deferred in regroup anchor_ids.",
        "properties": {
            "reviewed_ids": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "integer", "minimum": 1},
                "description": (
                    "Mutable memory ids kept unchanged."
                ),
            },
            "changes": {
                "type": "array",
                "description": (
                    "State changes for mutable memories."
                ),
                "items": {
                    "oneOf": [
                        {
                            "type": "object",
                            "description": (
                                "Replace one mutable row."
                            ),
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["replace"],
                                },
                                "memory_id": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "Target id from <mutable_memories>."
                                    ),
                                },
                                "content": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 2000,
                                    "description": (
                                        "Complete final content for the surviving row. "
                                        "Do not broaden the supported facts."
                                    ),
                                },
                                "activation": {
                                    "type": "string",
                                    "enum": ["always", "recall"],
                                    "description": (
                                        "Final activation."
                                    ),
                                },
                                "expires_at": {
                                    "type": "null",
                                    "description": (
                                        "Always null; memories do not expire. "
                                        "Temporary state belongs to current state."
                                    ),
                                },
                                "evidence": {
                                    "oneOf": [
                                        _EVIDENCE_SCHEMA,
                                        {"type": "null"},
                                    ],
                                    "description": (
                                        "Required exact owner evidence for any factual "
                                        "correction. Use null only if object, scope, "
                                        "conditions, duration and polarity are unchanged."
                                    ),
                                },
                                "reason": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 400,
                                    "description": (
                                        "Short audit reason explaining what changed "
                                        "and why."
                                    ),
                                },
                            },
                            "required": [
                                "action",
                                "memory_id",
                                "content",
                                "activation",
                                "expires_at",
                                "evidence",
                                "reason",
                            ],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "description": (
                                "Keep one mutable survivor and absorb the source rows."
                            ),
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["merge"],
                                },
                                "survivor_id": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "Mutable id to keep; excluded from source_ids."
                                    ),
                                },
                                "source_ids": {
                                    "type": "array",
                                    "minItems": 1,
                                    "uniqueItems": True,
                                    "items": {"type": "integer", "minimum": 1},
                                    "description": (
                                        "Other mutable ids absorbed by survivor_id. "
                                        "Each source is retired through superseded_by."
                                    ),
                                },
                                "content": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 2000,
                                    "description": (
                                        "Complete final survivor content."
                                    ),
                                },
                                "activation": {
                                    "type": "string",
                                    "enum": ["always", "recall"],
                                    "description": (
                                        "Final activation; always is legal only "
                                        "when every merged row was already always."
                                    ),
                                },
                                "expires_at": {
                                    "type": "null",
                                    "description": (
                                        "Always null; memories do not expire."
                                    ),
                                },
                                "evidence_event_ids": {
                                    "type": "array",
                                    "minItems": 1,
                                    "uniqueItems": True,
                                    "items": {"type": "string", "minLength": 1},
                                    "description": (
                                        "Exact event_id strings from <owner_evidence> "
                                        "supporting final content; copy verbatim."
                                    ),
                                },
                                "reason": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 400,
                                    "description": (
                                        "Short audit reason proving why these rows are "
                                        "one fact/event rather than merely related."
                                    ),
                                },
                            },
                            "required": [
                                "action",
                                "survivor_id",
                                "source_ids",
                                "content",
                                "activation",
                                "expires_at",
                                "evidence_event_ids",
                                "reason",
                            ],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "description": (
                                "Retire one mutable fact. Expired recent rows are purged by the runtime."
                            ),
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["retire"],
                                },
                                "memory_id": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "Mutable id to retire."
                                    ),
                                },
                                "evidence": _EVIDENCE_SCHEMA,
                                "reason": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 400,
                                    "description": (
                                        "Short audit reason naming the explicit owner "
                                        "revocation."
                                    ),
                                },
                            },
                            "required": [
                                "action",
                                "memory_id",
                                "evidence",
                                "reason",
                            ],
                            "additionalProperties": False,
                        },
                    ]
                },
            },
            "regroup_requests": {
                "type": "array",
                "description": (
                    "Defer anchors that need related read-only ids promoted into a later mutable group."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "anchor_ids": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "integer", "minimum": 1},
                            "description": (
                                "Mutable ids that must wait."
                            ),
                        },
                        "include_ids": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "integer", "minimum": 1},
                            "description": (
                                "Related ids found only in context/directory; they "
                                "must not already be mutable."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 400,
                            "description": "Why these ids require one later review group.",
                        },
                    },
                    "required": ["anchor_ids", "include_ids", "reason"],
                    "additionalProperties": False,
                },
            },
            "summary": {
                "type": "string",
                "maxLength": 500,
                "description": (
                    "Private audit summary of keeps, changes, and deferrals."
                ),
            },
        },
        "required": [
            "reviewed_ids",
            "changes",
            "regroup_requests",
            "summary",
        ],
        "additionalProperties": False,
    },
}
