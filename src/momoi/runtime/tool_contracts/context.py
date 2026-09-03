from typing import Any


NEW_EPISODE_REF = "new:<slug>"

RECALL_TOOL_SPEC: dict[str, Any] = {
    "name": "recall",
    "description": (
        "Mandatory first and only action at the start of every Owner Turn. Submit "
        "each independent intent's minimum historical scope and Episode decision. "
        "Returns confirmed memory, dated reflection, and Episode summaries; it does "
        "not answer the owner or perform the work."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "units": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "maxLength": 160,
                            "description": (
                                "Operative outcome for this unit, with corrections "
                                "folded into the final intent."
                            ),
                        },
                        "recall_mode": {
                            "type": "string",
                            "enum": ["search", "reuse"],
                            "description": (
                                "search for new/changed scope; reuse only a displayed "
                                "prior scope that fully covers this unit."
                            ),
                        },
                        "recall_queries": {
                            "type": "array",
                            "minItems": 0,
                            "maxItems": 3,
                            "description": (
                                "Fewest non-overlapping evidence needs; non-empty for "
                                "search, empty for reuse."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "semantic": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 240,
                                        "description": (
                                            "Self-contained declarative retrieval need "
                                            "using conversation-supported canonical "
                                            "subjects. Name the missing fact, relationship, "
                                            "convention, preference, interaction, or task "
                                            "state. Describe unresolved referents without "
                                            "guessing identity. No questions, copied chat, "
                                            "or guessed answers."
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
                            "description": (
                                "For reuse: a Turn in recent_recall_context. Empty for search."
                            ),
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
                                    "description": (
                                        "none by default; continue only if this Turn advances "
                                        "the same concrete experience; new only for a distinct "
                                        "experience worth retaining. Proximity, mood, time, or "
                                        "setting alone never establishes continuity."
                                    ),
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
            "Mandatory first action of an autonomous Heartbeat execution. Choose "
            "the real activity, its historical scope, the MCP groups needed for "
            "that activity, and a short evidence-dependent execution strategy. "
            "The runtime returns recalled evidence and enables selected tools."
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
                    "description": (
                        "One or two non-overlapping historical needs for search; "
                        "empty for skip."
                    ),
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
                    "items": {"type": "string", "enum": group_ids},
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
                        "For work, the minimum ordered checks, result branches and "
                        "completion or continuation condition. Empty for rest."
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
            "additionalProperties": False,
        },
    }
