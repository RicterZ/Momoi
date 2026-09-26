"""Single source for state mutation fields, limits and model-facing guidance.

The private maintenance workflow uses this schema and passes its arguments to
CurrentStateManager.apply_arguments. Foreground end_turn does not carry state.
"""

MAX_SLOTS = 12
SLOT_SOFT_WARNING_THRESHOLD = 8
MAX_TTL_SECONDS = 24 * 3600
SUBJECT_MAX_LENGTH = 128
KEY_MAX_LENGTH = 64
KEY_PATTERN = r"^[a-z][a-z0-9_.-]*(?![\s\S])"
VALUE_MAX_LENGTH = 512
ID_MAX_LENGTH = 128
CURRENT_STATE_SOURCE_STAGES = frozenset({"owner", "goal", "heartbeat", "webhook", "reply_followup", "plan_step"})
# Current-state maintenance is temporarily driven only by completed owner Turns.
# Keep CURRENT_STATE_SOURCE_STAGES broad because it also controls which stages
# may receive current-state context; this narrower set controls task staging.
CURRENT_STATE_TRIGGER_STAGES = frozenset({"owner"})

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
                "in this same change set, atomically. "
                "Use IDs from the current maintenance snapshot. "
                "The backend handles time-based expiry."
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
                    "status": {
                        "type": "string", "enum": ["observed", "inferred"],
                        "description": "Observed means directly stated by the source, not independently proven. Mark deductions inferred.",
                    },
                    "source_turn": {
                        "type": "string", "minLength": 1,
                        "description": "Exact T-N label of the evidence Turn in this transcript.",
                    },
                    "source": {
                        "type": "string", "minLength": 1, "maxLength": 512,
                        "description": "Exact contiguous quote from one source message. Never attribute assistant words to the owner. Runtime resolves speaker and evidence time.",
                    },
                    "uncertainty": {
                        "type": "string", "maxLength": 512,
                        "description": "What remains unconfirmed; required nonempty for inferred states, otherwise may be empty.",
                    },
                    "ttl_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_TTL_SECONDS,
                        "description": (
                            "How long new evidence justifies retaining this fact, starting at commit time. "
                            "If it needs to remain available longer than 24 hours, it is durable — "
                            "file it through memory_operation instead of a current-state slot. "
                            "Seeing or reusing a slot is not evidence for renewal. Expiry means unknown, "
                            "not that the opposite state holds. Renewal requires fresh evidence and delete + add."
                        ),
                    },
                },
                "required": ["subject", "key", "value", "ttl_seconds", "status", "source_turn", "source", "uncertainty"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["add", "delete"],
    "additionalProperties": False,
}
