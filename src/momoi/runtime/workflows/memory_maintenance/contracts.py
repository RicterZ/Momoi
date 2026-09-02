MAINTENANCE_ACTIONS = {"replace", "merge", "retire"}
MEMORY_MAINTENANCE_RUN_VERSION = "v1"

_EVIDENCE_SCHEMA = {
    "type": "object",
    "description": (
        "Exact authenticated owner evidence. Copy both fields verbatim from "
        "<owner_evidence>; never rewrite a channel prefix or paraphrase the quote."
    ),
    "properties": {
        "event_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Exact event_id from <owner_evidence>. Example: "
                "napcat:2167634556:123456789."
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
_FINGERPRINT_SCHEMA = {
    "type": "string",
    "pattern": "^sha256:[0-9a-f]{64}$",
    "description": (
        "Copy the supplied snapshot_fingerprint verbatim, including the sha256: "
        "prefix. Example: sha256:0123456789abcdef... (64 lowercase hex digits)."
    ),
}

MEMORY_MAINTENANCE_FINISH_SPEC: dict[str, object] = {
    "name": "memory_maintenance_finish",
    "description": (
        "Submit the only terminal result for one confirmed-memory maintenance "
        "batch. Every mutable id must appear exactly once: in reviewed_ids when "
        "unchanged, as a target of one change, or in regroup anchor_ids. Changed or "
        "deferred ids must not also appear in reviewed_ids."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "version": {
                "type": "integer",
                "enum": [1],
                "description": "Protocol version; always 1.",
            },
            "reviewed_ids": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "integer", "minimum": 1},
                "description": (
                    "Mutable memory ids kept exactly unchanged. Do not include ids "
                    "targeted by replace/merge/retire or deferred by regroup. "
                    "Example: [3, 4, 9]."
                ),
            },
            "changes": {
                "type": "array",
                "description": (
                    "Validated state changes. Use replace for one row, merge for true "
                    "duplicates or facets of one concrete temporary event, and retire "
                    "only for explicit owner revocation. Example: [] when no changes."
                ),
                "items": {
                    "oneOf": [
                        {
                            "type": "object",
                            "description": (
                                "Replace one mutable row when correcting a fact, "
                                "removing turn-dependent wording, or moving it to the "
                                "right activation. `evidence` may be null only when "
                                "facts are unchanged (wording/classification only). "
                                "Example: replace memory 64 from always to recall with "
                                "the same content and evidence=null."
                            ),
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["replace"],
                                    "description": "Literal discriminator: replace.",
                                },
                                "memory_id": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "One id from <mutable_memories>; omit it from "
                                        "reviewed_ids."
                                    ),
                                },
                                "snapshot_fingerprint": {
                                    **_FINGERPRINT_SCHEMA,
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
                                    "enum": ["always", "recent", "recall"],
                                    "description": (
                                        "Complete final activation. Never promote a "
                                        "non-always row to always. Topic-scoped game, "
                                        "device, tool and procedure rules are recall."
                                    ),
                                },
                                "expires_at": {
                                    "type": ["number", "null"],
                                    "description": (
                                        "Unix timestamp only for recent; otherwise "
                                        "null. Never invent an expiry."
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
                                "snapshot_fingerprint",
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
                                "Merge two or more mutable rows that are true "
                                "duplicates or facets of one concrete temporary event. "
                                "Keep one survivor and absorb every source. Example: "
                                "survivor_id=194, source_ids=[195,196], activation="
                                "recent, expires_at=the latest source expiry."
                            ),
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["merge"],
                                    "description": "Literal discriminator: merge.",
                                },
                                "survivor_id": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "Mutable id to keep. It must not occur in "
                                        "source_ids or reviewed_ids."
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
                                "snapshot_fingerprints": {
                                    "type": "object",
                                    "additionalProperties": {
                                        **_FINGERPRINT_SCHEMA,
                                    },
                                    "description": (
                                        "Exactly one entry for survivor_id and every "
                                        "source_id. Keys are decimal id strings; values "
                                        "are copied verbatim. Example: "
                                        '{"194":"sha256:...","195":"sha256:..."}.'
                                    ),
                                },
                                "content": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 2000,
                                    "description": (
                                        "Complete final survivor content. For true "
                                        "duplicates keep overlapping claims; for facets "
                                        "of a temporary event rebuild only from cited "
                                        "owner evidence."
                                    ),
                                },
                                "activation": {
                                    "type": "string",
                                    "enum": ["always", "recent", "recall"],
                                    "description": (
                                        "Complete final activation derived from final "
                                        "content, not inherited. `always` is legal only "
                                        "when every merged row was already always."
                                    ),
                                },
                                "expires_at": {
                                    "type": ["number", "null"],
                                    "description": (
                                        "For a merged recent event use the latest "
                                        "applicable source expiry; otherwise null."
                                    ),
                                },
                                "evidence_event_ids": {
                                    "type": "array",
                                    "minItems": 1,
                                    "uniqueItems": True,
                                    "items": {"type": "string", "minLength": 1},
                                    "description": (
                                        "Exact event_id strings from <owner_evidence> "
                                        "supporting final content. Copy prefixes "
                                        "verbatim; never change napcat to qq."
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
                                "snapshot_fingerprints",
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
                                "Retire one mutable fact only when an exact owner "
                                "quote explicitly revokes or contradicts it. Do not "
                                "retire duplicates (merge them) or expired recent "
                                "rows (runtime purges them)."
                            ),
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["retire"],
                                    "description": "Literal discriminator: retire.",
                                },
                                "memory_id": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "One mutable id to tombstone; omit it from "
                                        "reviewed_ids."
                                    ),
                                },
                                "snapshot_fingerprint": {
                                    **_FINGERPRINT_SCHEMA,
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
                                "snapshot_fingerprint",
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
                    "Use only when a mutable anchor needs a related id that is "
                    "currently read-only in context/directory. Do not regroup ids "
                    "already mutable; decide them now. Example: anchor_ids=[101], "
                    "include_ids=[114]."
                ),
                "items": {
                    "type": "object",
                    "description": (
                        "Defer mutable anchors until the listed external ids can be "
                        "promoted into one later mutable group."
                    ),
                    "properties": {
                        "anchor_ids": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "integer", "minimum": 1},
                            "description": (
                                "Mutable ids that must wait; omit them from "
                                "reviewed_ids and changes."
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
                    "Private audit summary of keeps, changes and deferrals. It is not "
                    "owner-visible. Example: 'Merged 13 into 39; kept 15 unchanged.'"
                ),
            },
        },
        "required": [
            "version",
            "reviewed_ids",
            "changes",
            "regroup_requests",
            "summary",
        ],
        "additionalProperties": False,
    },
}
