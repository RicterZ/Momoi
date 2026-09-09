"""Single source for state mutation fields, limits and model-facing guidance.

The private maintenance workflow uses this schema and passes its arguments to
CurrentStateManager.apply_arguments. Foreground end_turn does not carry state.
"""

MAX_SLOTS = 128
MAX_TTL_SECONDS = 24 * 3600
SUBJECT_MAX_LENGTH = 128
KEY_MAX_LENGTH = 64
KEY_PATTERN = r"^[a-z][a-z0-9_.-]*(?![\s\S])"
VALUE_MAX_LENGTH = 512
ID_MAX_LENGTH = 128
CURRENT_STATE_SOURCE_STAGES = frozenset({"owner", "goal", "heartbeat", "webhook", "reply_followup"})

CURRENT_STATE_CHANGE_SCHEMA = {
    "type": "object",
    "description": (
        "Return empty add and delete arrays when nothing changes. "
        "Long-term preferences belong to memory; scheduled work belongs to goals. "
        "All changes are validated and committed atomically."
    ),
    "properties": {
        "delete": {
            "type": "array",
            "maxItems": MAX_SLOTS,
            "uniqueItems": True,
            "description": (
                "IDs of existing slots contradicted or ended by new evidence. "
                "A replacement requires deleting the old slot and adding its successor "
                "in this same change set. The backend handles time-based expiry."
            ),
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": ID_MAX_LENGTH,
                "pattern": r"\S",
            },
        },
        "add": {
            "type": "array",
            "maxItems": MAX_SLOTS,
            "description": "New evidence-supported states; reuse an existing dimension when applicable.",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": SUBJECT_MAX_LENGTH,
                        "pattern": r"\S",
                        "description": "The known state holder. Distinguish owner, assistant and other established entities.",
                    },
                    "key": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": KEY_MAX_LENGTH,
                        "pattern": KEY_PATTERN,
                        "description": (
                            "State dimension. Reuse existing keys instead of inventing synonyms. "
                            "Only one active slot per subject and key is allowed."
                        ),
                    },
                    "value": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": VALUE_MAX_LENGTH,
                        "pattern": r"\S",
                        "description": "A concise current fact supported by the supplied evidence, preserving attribution and uncertainty.",
                    },
                    "ttl_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_TTL_SECONDS,
                        "description": (
                            "How long new evidence justifies retaining this fact, starting at commit time. "
                            "If it needs to remain available longer than 24 hours, create a recent memory "
                            "(activation=recent) through the memory workflow instead of a current-state slot. "
                            "Seeing or reusing a slot is not evidence for renewal. Expiry means unknown, "
                            "not that the opposite state holds. Renewal requires fresh evidence and delete + add."
                        ),
                    },
                },
                "required": ["subject", "key", "value", "ttl_seconds"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["add", "delete"],
    "additionalProperties": False,
}
