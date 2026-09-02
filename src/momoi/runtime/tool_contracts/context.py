from typing import Any


NEW_EPISODE_REF = "new:<slug>"

RECALL_TOOL_SPEC: dict[str, Any] = {
    "name": "recall",
    "description": (
        "Mandatory first action of every Owner Turn. Submit the minimum complete "
        "historical scope for each independent intent and its Episode disposition. "
        "The runtime returns confirmed memory, dated reflection and Episode "
        "summaries. This call selects context; it does not answer the owner or "
        "perform the requested work."
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
                                "Brief operative outcome for this independent unit. "
                                "Fold corrections into the final intended outcome."
                            ),
                        },
                        "recall_mode": {
                            "type": "string",
                            "enum": ["search", "reuse"],
                            "description": (
                                "search for a new or changed historical scope; reuse "
                                "only a displayed prior scope that already covers this "
                                "one completely."
                            ),
                        },
                        "recall_queries": {
                            "type": "array",
                            "minItems": 0,
                            "maxItems": 3,
                            "description": (
                                "Required and non-empty for search; empty for reuse. "
                                "Use the fewest non-overlapping evidence needs."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "semantic": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 240,
                                        "description": (
                                            "One self-contained declarative retrieval "
                                            "need using canonical subjects supported by "
                                            "the conversation. If an identity remains "
                                            "unresolved, describe that missing referent "
                                            "without assigning a guessed identity. Name "
                                            "the missing fact, "
                                            "relationship, convention, preference, prior "
                                            "interaction or task state. Do not write a "
                                            "question, copy conversational wording, or "
                                            "include a guessed answer."
                                        ),
                                    },
                                    "keywords": {
                                        "type": "array",
                                        "minItems": 0,
                                        "maxItems": 6,
                                        "items": {"type": "string", "maxLength": 60},
                                        "description": (
                                            "Independent sparse OR anchors: literal "
                                            "canonical names, identifiers, titles or "
                                            "exact phrases only. Omit verbs, pronouns, "
                                            "generic words and inferred answer terms; "
                                            "leave empty when no reliable anchor exists."
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
                                "Required for reuse and empty for search. Must be a "
                                "Turn shown in recent_recall_context."
                            ),
                        },
                        "episode": {
                            "type": "object",
                            "description": (
                                "Independent archival decision for this unit; it never "
                                "changes whether recall is search or reuse."
                            ),
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["none", "continue", "new"],
                                    "description": (
                                        "none by default; continue only when this Turn "
                                        "directly advances the same concrete experience; "
                                        "new only for a distinct experience worth retaining. "
                                        "Conversation proximity, shared mood, time or "
                                        "setting do not establish continuity."
                                    ),
                                },
                                "ref": {
                                    "type": "string",
                                    "description": (
                                        "Existing candidate Episode id for continue; "
                                        f"{NEW_EPISODE_REF} chosen for new; empty for "
                                        "none."
                                    ),
                                },
                                "title": {
                                    "type": "string",
                                    "maxLength": 80,
                                    "description": (
                                        "Specific experience title for new; empty "
                                        "otherwise."
                                    ),
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

