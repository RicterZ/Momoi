import json



class ContextPlanError(ValueError):
    pass


SPEECH_ACTS = {
    "request",
    "question",
    "correction",
    "emotional_share",
    "casual_share",
    "banter",
    "acknowledgment",
    "closing",
    "unknown",
}
NEW_EPISODE_REF_FORMAT = "new:[a-z0-9][a-z0-9_-]{0,39}"
RECALL_NEED_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "semantic": {
            "type": "string",
            "minLength": 1,
            "maxLength": 180,
            "description": (
                "One concise, self-contained declarative proposition describing "
                "the historical fact, relationship, preference, convention, method, "
                "rationale, or prior event to retrieve for semantic search. Omit "
                "conversational setup, response use, questions, speculative answers, "
                "and alternative lists; do not copy the owner's utterance or "
                "prescribe a response."
            ),
        },
        "keywords": {
            "type": "array",
            "maxItems": 8,
            "uniqueItems": True,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": 60,
            },
            "description": (
                "Zero or more literal names, identifiers, titles, exact phrases, or "
                "genuine aliases for sparse search. Each item is one independent OR "
                "alternative; do not put `|` inside an item and do not add generic or "
                "inferred answer words. Empty is valid when no selective literal "
                "anchor is known."
            ),
        },
    },
    "required": ["semantic", "keywords"],
    "additionalProperties": False,
}


HEARTBEAT_PLAN_TOOL_NAME = "submit_heartbeat_plan"
HEARTBEAT_PLAN_TOOL_SPEC: dict[str, object] = {
    "name": HEARTBEAT_PLAN_TOOL_NAME,
    "description": (
        "Submit the selected heartbeat activity and advisory execution handoff."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "version": {"type": "integer", "enum": [3]},
            "activity": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "maxLength": 300},
                    "reason": {"type": "string", "maxLength": 300},
                    "recall_mode": {
                        "type": "string",
                        "enum": ["search", "skip"],
                        "description": (
                            "Prefer search whenever plausible unsupplied history "
                            "could improve continuity, personalization, novelty, "
                            "activity choice, or execution, or prevent contradiction, "
                            "repetition, or repeated work. Skip only when all such "
                            "history is supplied or history clearly cannot matter."
                        ),
                    },
                    "recall_queries": {
                        "type": "array",
                        "maxItems": 3,
                        "uniqueItems": True,
                        "items": RECALL_NEED_SCHEMA,
                        "description": (
                            "For search, one to three ranked retrieval needs. `semantic` "
                            "is a self-contained dense-query rewrite of the historical "
                            "evidence sought; `keywords` contains only literal sparse "
                            "anchors. Separate facets that historical records could "
                            "satisfy independently. Empty only for skip."
                        ),
                    },
                },
                "required": [
                    "intent",
                    "reason",
                    "recall_mode",
                    "recall_queries",
                ],
                "additionalProperties": False,
            },
            "heartbeat_handoff": {
                "type": "object",
                "properties": {
                    "context": {
                        "type": "object",
                        "properties": {
                            "status": {
                                "type": "string",
                                "enum": ["sufficient", "lookup_required"],
                            },
                            "needs": {
                                "type": "array",
                                "maxItems": 2,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "tool": {
                                            "type": "string",
                                            "enum": [
                                                "memory_search",
                                                "episode_search",
                                                "episode_read",
                                            ],
                                        },
                                        "query": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 300,
                                        },
                                        "evidence": {
                                            "type": "string",
                                            "enum": [
                                                "exact_wording",
                                                "chronology",
                                                "unresolved_reference",
                                                "correction_evidence",
                                                "relevant_history",
                                            ],
                                        },
                                    },
                                    "required": ["tool", "query", "evidence"],
                                    "additionalProperties": False,
                                },
                            },
                            "reason": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 300,
                            },
                        },
                        "required": ["status", "needs", "reason"],
                        "additionalProperties": False,
                    },
                    "mcp": {
                        "type": "object",
                        "properties": {
                            "servers": {
                                "type": "array",
                                "maxItems": 32,
                                "items": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 100,
                                },
                            },
                            "reason": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 300,
                            },
                        },
                        "required": ["servers", "reason"],
                        "additionalProperties": False,
                    },
                    "execution": {
                        "type": "object",
                        "properties": {
                            "mode": {
                                "type": "string",
                                "enum": ["rest", "work"],
                            },
                            "outline": {
                                "type": "array",
                                "maxItems": 4,
                                "items": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 300,
                                },
                            },
                            "reason": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 300,
                            },
                        },
                        "required": ["mode", "outline", "reason"],
                        "additionalProperties": False,
                    },
                },
                "required": ["context", "mcp", "execution"],
                "additionalProperties": False,
            },
            "uncertainty": {
                "type": "array",
                "maxItems": 4,
                "items": {"type": "string", "maxLength": 500},
            },
        },
        "required": [
            "version",
            "activity",
            "heartbeat_handoff",
            "uncertainty",
        ],
        "additionalProperties": False,
    },
}


def _strings(
    value: object,
    name: str,
    *,
    minimum: int = 0,
    maximum: int,
    max_length: int,
) -> list[str]:
    if (
        not isinstance(value, list)
        or not minimum <= len(value) <= maximum
        or any(
            not isinstance(item, str)
            or not item.strip()
            or len(item.strip()) > max_length
            for item in value
        )
    ):
        raise ContextPlanError(f"invalid_{name}")
    return [str(item).strip() for item in value]


def _text(value: object, name: str, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > max_length
    ):
        raise ContextPlanError(f"invalid_{name}")
    return value.strip()


def _recall_queries(
    value: object,
    name: str,
    *,
    minimum: int = 1,
    maximum: int = 3,
) -> list[dict[str, object]]:
    """Validate semantic rewrites separately from literal sparse anchors."""

    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ContextPlanError(f"invalid_{name}")
    queries: list[dict[str, object]] = []
    identities: set[tuple[str, tuple[str, ...]]] = set()
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {"semantic", "keywords"}:
            raise ContextPlanError(f"invalid_{name}")
        semantic = _text(raw["semantic"], f"{name}_semantic", 180)
        keywords = _strings(
            raw["keywords"],
            f"{name}_keywords",
            maximum=8,
            max_length=60,
        )
        keywords = [" ".join(keyword.split()) for keyword in keywords]
        if any("|" in keyword or "｜" in keyword for keyword in keywords):
            raise ContextPlanError(f"invalid_{name}_keyword")
        if len(set(keywords)) != len(keywords):
            raise ContextPlanError(f"duplicate_{name}_keyword")
        identity = (semantic, tuple(keywords))
        if identity in identities:
            raise ContextPlanError(f"duplicate_{name}")
        identities.add(identity)
        queries.append({"semantic": semantic, "keywords": keywords})
    return queries


def _parse_mcp_route(
    raw: object,
    available_mcp_servers: set[str] | None,
) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != {"servers", "reason"}:
        raise ContextPlanError("invalid_mcp_route")
    servers = _strings(
        raw["servers"],
        "mcp_servers",
        maximum=32,
        max_length=100,
    )
    if len(set(servers)) != len(servers):
        raise ContextPlanError("duplicate_mcp_server")
    if available_mcp_servers is not None and not set(servers) <= available_mcp_servers:
        raise ContextPlanError("unknown_mcp_server")
    return {
        "servers": servers,
        "reason": _text(raw["reason"], "mcp_reason", 300),
    }


def _parse_context_needs(
    raw_needs: object,
    *,
    tools: set[str],
    evidence: set[str],
    error: str,
    thinking_requires_past_reasoning: bool = False,
) -> list[dict[str, str]]:
    if not isinstance(raw_needs, list) or len(raw_needs) > 2:
        raise ContextPlanError(error)
    needs: list[dict[str, str]] = []
    for raw_need in raw_needs:
        if not isinstance(raw_need, dict) or set(raw_need) != {
            "tool",
            "query",
            "evidence",
        }:
            raise ContextPlanError(error)
        tool = raw_need["tool"]
        need_evidence = raw_need["evidence"]
        if tool not in tools or need_evidence not in evidence:
            raise ContextPlanError(error)
        if (
            thinking_requires_past_reasoning
            and str(tool).startswith("thinking_")
            and need_evidence != "past_reasoning"
        ):
            raise ContextPlanError("invalid_thinking_context_need")
        needs.append(
            {
                "tool": str(tool),
                "query": _text(raw_need["query"], "context_need_query", 300),
                "evidence": str(need_evidence),
            }
        )
    return needs


def _parse_context_block(
    raw: object,
    *,
    tools: set[str],
    evidence: set[str],
    error: str,
    need_error: str,
    thinking_requires_past_reasoning: bool = False,
) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != {"status", "needs", "reason"}:
        raise ContextPlanError(error)
    status = raw["status"]
    raw_needs = raw["needs"]
    if (
        status not in {"sufficient", "lookup_required"}
        or not isinstance(raw_needs, list)
        or len(raw_needs) > 2
        or (status == "sufficient" and raw_needs)
        or (status == "lookup_required" and not raw_needs)
    ):
        raise ContextPlanError(error)
    needs = _parse_context_needs(
        raw_needs,
        tools=tools,
        evidence=evidence,
        error=need_error,
        thinking_requires_past_reasoning=thinking_requires_past_reasoning,
    )
    return {
        "status": status,
        "needs": needs,
        "reason": _text(raw["reason"], "context_reason", 300),
    }


def parse_heartbeat_plan(
    value: str | dict[str, object],
    available_mcp_servers: set[str] | None = None,
) -> dict[str, object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError) as error:
            raise ContextPlanError("invalid_json") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "activity", "heartbeat_handoff", "uncertainty"}
        or value.get("version") != 3
    ):
        raise ContextPlanError("invalid_heartbeat_plan")
    activity = value.get("activity")
    if not isinstance(activity, dict) or set(activity) != {
        "intent",
        "reason",
        "recall_mode",
        "recall_queries",
    }:
        raise ContextPlanError("invalid_heartbeat_activity")
    raw_handoff = value.get("heartbeat_handoff")
    if not isinstance(raw_handoff, dict) or set(raw_handoff) != {
        "context",
        "mcp",
        "execution",
    }:
        raise ContextPlanError("invalid_heartbeat_handoff")
    context = _parse_context_block(
        raw_handoff["context"],
        tools={
            "memory_search",
            "episode_search",
            "episode_read",
        },
        evidence={
            "exact_wording",
            "chronology",
            "unresolved_reference",
            "correction_evidence",
            "relevant_history",
        },
        error="invalid_heartbeat_context",
        need_error="invalid_heartbeat_context_need",
    )
    mcp_route = _parse_mcp_route(raw_handoff["mcp"], available_mcp_servers)
    raw_execution = raw_handoff["execution"]
    if not isinstance(raw_execution, dict) or set(raw_execution) != {
        "mode",
        "outline",
        "reason",
    }:
        raise ContextPlanError("invalid_heartbeat_execution")
    mode = raw_execution["mode"]
    outline = _strings(
        raw_execution["outline"],
        "heartbeat_execution_outline",
        maximum=4,
        max_length=300,
    )
    servers = mcp_route["servers"]
    if (
        mode not in {"rest", "work"}
        or (
            mode == "rest"
            and (
                outline
                or context["status"] != "sufficient"
                or context["needs"]
                or servers
            )
        )
        or (mode == "work" and not outline)
    ):
        raise ContextPlanError("invalid_heartbeat_execution")

    recall_mode = activity["recall_mode"]
    recall_queries = _recall_queries(
        activity["recall_queries"],
        "heartbeat_recall_queries",
        minimum=0,
        maximum=3,
    )
    if (
        recall_mode == "search"
        and not recall_queries
        or recall_mode == "skip"
        and recall_queries
        or recall_mode not in {"search", "skip"}
    ):
        raise ContextPlanError("invalid_heartbeat_recall_decision")
    parsed_activity = {
        "intent": _text(activity["intent"], "heartbeat_intent", 300),
        "reason": _text(activity["reason"], "heartbeat_reason", 300),
        "recall_mode": str(recall_mode),
        "recall_queries": recall_queries,
    }
    return {
        "version": 3,
        "activity": parsed_activity,
        "heartbeat_handoff": {
            "context": context,
            "mcp": mcp_route,
            "execution": {
                "mode": str(mode),
                "outline": outline,
                "reason": _text(
                    raw_execution["reason"],
                    "heartbeat_execution_reason",
                    300,
                ),
            },
        },
        "uncertainty": _strings(
            value["uncertainty"],
            "heartbeat_uncertainty",
            maximum=4,
            max_length=500,
        ),
    }


def degraded_heartbeat_plan(activity: str, reason: str) -> dict[str, object]:
    return {
        "version": 3,
        "activity": {
            "intent": activity.strip() or "spend time freely",
            "reason": f"Heartbeat planner failed ({reason}); continue current activity.",
            "recall_mode": "skip",
            "recall_queries": [],
        },
        "heartbeat_handoff": {
            "context": {
                "status": "sufficient",
                "needs": [],
                "reason": (
                    f"Heartbeat planner failed ({reason}); no historical lookup "
                    "is assumed."
                ),
            },
            "mcp": {
                "servers": [],
                "reason": (
                    f"Heartbeat planner failed ({reason}); no external MCP server "
                    "is preloaded."
                ),
            },
            "execution": {
                "mode": "rest",
                "outline": [],
                "reason": (
                    f"Heartbeat planner failed ({reason}); preserve the current "
                    "state without manufacturing work."
                ),
            },
        },
        "uncertainty": [f"Heartbeat planner protocol failed: {reason}"],
    }
