from typing import Any


NEW_EPISODE_REF = "new:<slug>"

RECALL_TOOL_SPEC: dict[str, Any] = {
    "name": "recall",
    "description": (
        "Retrieve confirmed memory, dated reflection, and Episode summaries for "
        "the Owner Turn, and bind its archival Episode membership."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "units": {
                "type": "array",
                "description": "One unit per independent owner intent.",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "maxLength": 160,
                            "description": (
                                "Objectively describe the owner's current request or "
                                "shared information, incorporating corrections. "
                                "Preserve uncertainty; do not add unstated needs or "
                                "your intended response strategy."
                            ),
                        },
                        "recall_mode": {
                            "type": "string",
                            "enum": ["search", "reuse", "skip"],
                            "description": (
                                "search for missing historical evidence; reuse a prior "
                                "query scope; skip when supplied context is sufficient."
                            ),
                        },
                        "recall_queries": {
                            "type": "array",
                            "minItems": 0,
                            "maxItems": 3,
                            "description": (
                                "Non-overlapping historical evidence needs."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "semantic": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 240,
                                        "description": (
                                            "Objectively describe the historical information "
                                            "missing from supplied context, using supported "
                                            "subjects and preserving unresolved references. "
                                            "Do not embed a presumed answer, inferred owner "
                                            "preference, or intended response strategy."
                                        ),
                                    },
                                    "keywords": {
                                        "type": "array",
                                        "minItems": 0,
                                        "maxItems": 6,
                                        "items": {"type": "string", "maxLength": 60},
                                        "description": (
                                            "Sparse OR anchors: literal canonical names, "
                                            "IDs, titles, or exact phrases. No verbs, "
                                            "pronouns, generic words, or inferred answers; "
                                            "empty if no reliable anchor exists."
                                        ),
                                    },
                                },
                                "required": ["semantic"],
                                "additionalProperties": False,
                            },
                        },
                        "recall_from_turn_id": {
                            "type": "string",
                            "description": ("Source Turn in recent_recall_context."),
                        },
                        "episode": {
                            "type": "object",
                            "description": (
                                "Independent archival decision; does not affect recall_mode."
                            ),
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["none", "continue", "new"],
                                },
                                "ref": {
                                    "type": "string",
                                    "description": (
                                        "Candidate Episode id for continue; "
                                        f"{NEW_EPISODE_REF} for new; empty for none."
                                    ),
                                },
                                "title": {
                                    "type": "string",
                                    "maxLength": 80,
                                    "description": "Specific title for new; otherwise empty.",
                                },
                            },
                            "required": ["action"],
                            "additionalProperties": False,
                        },
                    },
                    "required": [
                        "intent",
                        "recall_mode",
                        "recall_queries",
                        "recall_from_turn_id",
                        "episode",
                    ],
                    "oneOf": [
                        {
                            "properties": {
                                "recall_mode": {"enum": ["search"]},
                                "recall_queries": {"minItems": 1},
                                "recall_from_turn_id": {"const": ""},
                            }
                        },
                        {
                            "properties": {
                                "recall_mode": {"enum": ["reuse"]},
                                "recall_queries": {"maxItems": 0},
                                "recall_from_turn_id": {"minLength": 1},
                            }
                        },
                        {
                            "properties": {
                                "recall_mode": {"enum": ["skip"]},
                                "recall_queries": {"maxItems": 0},
                                "recall_from_turn_id": {"const": ""},
                            }
                        },
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["units"],
        "additionalProperties": False,
    },
}


def heartbeat_begin_spec(group_descriptions: dict[str, str]) -> dict[str, Any]:
    groups = {
        group: str(description).strip()
        for group, description in sorted(group_descriptions.items())
    }
    group_ids = list(groups)
    return {
        "name": "heartbeat_begin",
        "description": (
            "Begin the chosen autonomous activity; retrieve its historical evidence "
            "and enable the selected MCP groups."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "activity": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                    "description": (
                        "What Momoi will genuinely do or experience in this Heartbeat."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["work", "rest"],
                },
                "recall_mode": {
                    "type": "string",
                    "enum": ["search", "skip"],
                    "description": (
                        "Search only when history can change activity choice or "
                        "execution; skip when it cannot."
                    ),
                },
                "recall_queries": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 2,
                    "description": ("Non-overlapping historical evidence needs."),
                    "items": {
                        "type": "object",
                        "properties": {
                            "semantic": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 240,
                            },
                            "keywords": {
                                "type": "array",
                                "maxItems": 6,
                                "items": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 60,
                                },
                            },
                        },
                        "required": ["semantic", "keywords"],
                        "additionalProperties": False,
                    },
                },
                "tool_groups": {
                    "type": "array",
                    "maxItems": len(group_ids),
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        **({"enum": group_ids} if group_ids else {}),
                    },
                    "description": (
                        "MCP groups required by the chosen activity. "
                        + "; ".join(
                            f"{group}: {description}"
                            for group, description in groups.items()
                        )
                    ),
                },
                "strategy": {
                    "type": "array",
                    "maxItems": 4,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 300,
                    },
                    "description": (
                        "Minimum ordered checks, result branches, and completion "
                        "or continuation condition."
                    ),
                },
            },
            "required": [
                "activity",
                "mode",
                "recall_mode",
                "recall_queries",
                "tool_groups",
                "strategy",
            ],
            "allOf": [
                {
                    "oneOf": [
                        {
                            "properties": {
                                "recall_mode": {"enum": ["search"]},
                                "recall_queries": {"minItems": 1},
                            }
                        },
                        {
                            "properties": {
                                "recall_mode": {"enum": ["skip"]},
                                "recall_queries": {"maxItems": 0},
                            }
                        },
                    ]
                },
                {
                    "oneOf": [
                        {
                            "properties": {
                                "mode": {"enum": ["work"]},
                                "strategy": {"minItems": 1},
                            }
                        },
                        {
                            "properties": {
                                "mode": {"enum": ["rest"]},
                                "strategy": {"maxItems": 0},
                            }
                        },
                    ]
                },
            ],
            "additionalProperties": False,
        },
    }
