import json
import re
import uuid

from ..contracts import ContextPlan


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
CONTEXT_PLAN_TOOL_NAME = "submit_context_plan"
NEW_EPISODE_REF_FORMAT = "new:[a-z0-9][a-z0-9_-]{0,39}"


def context_plan_correction(reason: str) -> str:
    guidance = {
        "missing_new_episode_ref": (
            "action=new requires episode_ref matching "
            f"{NEW_EPISODE_REF_FORMAT} and a non-empty title"
        ),
        "invalid_new_episode_ref": (
            "for action=new, episode_ref must match "
            f"{NEW_EPISODE_REF_FORMAT}; for example, new:morning-alice"
        ),
        "missing_new_episode_title": (
            "action=new requires a non-empty title and episode_ref matching "
            f"{NEW_EPISODE_REF_FORMAT}"
        ),
        "missing_continue_episode_ref": (
            "action=continue requires episode_ref equal to one supplied "
            "candidate Episode id"
        ),
    }
    return guidance.get(reason, reason)


def _has_directed_cycle(edges: list[tuple[str, str]]) -> bool:
    graph: dict[str, set[str]] = {}
    for source, target in edges:
        graph.setdefault(source, set()).add(target)
        graph.setdefault(target, set())
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(target) for target in graph.get(node, ())):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)


CONTEXT_PLAN_TOOL_SPEC: dict[str, object] = {
    "name": CONTEXT_PLAN_TOOL_NAME,
    "description": "Submit the complete context plan for the current owner input.",
    "input_schema": {
        "type": "object",
        "properties": {
            "version": {"type": "integer", "enum": [5]},
            "intent_units": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "description": (
                    "One item per independent operative goal. Fold a typo, "
                    "fragment, or corrected message into the final goal; never "
                    "create an empty, filler, revoked, or duplicate unit. Repeat "
                    "an event id across units only when that event genuinely "
                    "contains multiple independent goals. Verification, result "
                    "reporting, delivery, and failure handling for another goal "
                    "belong in strategy or completion criteria, not another unit."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "event_ids": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "string"},
                        },
                        "text": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 2000,
                        },
                        "intent": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                            "description": (
                                "Concise operative intent or desired outcome, not "
                                "a response outline, sequence, wording, style, or "
                                "list of advice."
                            ),
                        },
                        "speech_act": {
                            "type": "string",
                            "enum": sorted(SPEECH_ACTS - {"unknown"}),
                        },
                        "references": {
                            "type": "array",
                            "maxItems": 8,
                            "uniqueItems": True,
                            "items": {"type": "string"},
                        },
                        "recall_mode": {
                            "type": "string",
                            "enum": ["search", "reuse"],
                            "description": (
                                "Required; there is no skip. Decide recall before and "
                                "independently of Episode action. Reuse only when a "
                                "listed Turn's displayed queries explicitly cover the "
                                "complete current historical retrieval scope. Conversation "
                                "adjacency, a resolved immediate referent, the same scene, "
                                "or the same Episode does not establish coverage. Search "
                                "for every new, shifted, unresolved, missed, or degraded "
                                "subject or historical facet."
                            ),
                        },
                        "recall_queries": {
                            "type": "array",
                            "maxItems": 3,
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 120,
                            },
                            "description": (
                                "Ranked exact-word expressions for search; empty for "
                                "reuse. Emit the fewest distinct needs; one precise query "
                                "is normal. Each item is a distinct retrieval need; order "
                                "affects ranking only. Put the owner's explicit subject "
                                "or historical premise first. Prefer literal names, "
                                "genuine aliases, ambiguous owner wording, or a concise "
                                "subject-plus-history-facet anchor. If an emotion or "
                                "elliptical expression has no supplied cause or referent "
                                "and prior continuity could resolve it, its literal "
                                "wording is a valid query; if supplied context already "
                                "resolves it, do not search analogous past expressions. "
                                "Within one need, `|` joins only interchangeable, "
                                "parallel, equally weighted exact keywords without "
                                "spaces. Use separate items for distinct needs; do not "
                                "use a sentence, question, interpretation, planned "
                                "response, or overlapping query."
                            ),
                        },
                        "recall_from_turn_id": {
                            "type": "string",
                            "maxLength": 64,
                            "description": (
                                "Exact eligible Turn id displayed in "
                                "recent_recall_context when recall_mode is reuse; empty "
                                "for search."
                            ),
                        },
                    },
                    "required": [
                        "id",
                        "event_ids",
                        "text",
                        "intent",
                        "speech_act",
                        "references",
                        "recall_mode",
                        "recall_queries",
                        "recall_from_turn_id",
                    ],
                    "additionalProperties": False,
                },
            },
            "episode_actions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "description": (
                        "Choose exactly one action shape. none: action and "
                        "unit_ids only. continue: also include episode_ref equal "
                        "to a supplied candidate Episode id. new: also include "
                        "episode_ref matching new:[a-z0-9][a-z0-9_-]{0,39} and "
                        "a non-empty title. Metadata fields are optional and "
                        "default empty or zero."
                    ),
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["none", "continue", "new"],
                            "description": (
                                "none creates no Episode binding; continue binds "
                                "to a supplied candidate; new creates an Episode."
                            ),
                        },
                        "episode_ref": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                            "description": (
                                "Omit for none. Required for continue and must "
                                "equal a supplied candidate Episode id. Required "
                                "for new and must match "
                                "new:[a-z0-9][a-z0-9_-]{0,39}, for example "
                                "new:morning-alice."
                            ),
                        },
                        "title": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                            "description": (
                                "Required and non-empty for new. Omit for none; "
                                "a title supplied for continue is ignored."
                            ),
                        },
                        "unit_ids": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                            "description": (
                                "Intent unit ids assigned to this action; every "
                                "unit must appear in exactly one Episode action."
                            ),
                        },
                        "topics": {
                            "type": "array",
                            "maxItems": 12,
                            "items": {"type": "string"},
                        },
                        "entities": {
                            "type": "array",
                            "maxItems": 20,
                            "items": {"type": "string"},
                        },
                        "open_loops": {
                            "type": "array",
                            "maxItems": 8,
                            "items": {"type": "string"},
                        },
                        "salience": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                    },
                    "required": ["action", "unit_ids"],
                    "additionalProperties": False,
                },
            },
            "episode_links": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "from_episode_ref": {
                            "type": "string",
                            "description": (
                                "episode_ref of a new or continued Episode bound "
                                "by this Turn."
                            ),
                        },
                        "to_episode_ref": {
                            "type": "string",
                            "description": (
                                "episode_ref bound by this Turn or an exact "
                                "supplied candidate Episode id."
                            ),
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["continues", "references", "supersedes"],
                        },
                    },
                    "required": ["from_episode_ref", "to_episode_ref", "kind"],
                    "additionalProperties": False,
                },
            },
            "handoff": {
                "type": "object",
                "description": (
                    "Task-level advisory strategy for the downstream Owner. "
                    "Use exactly the declared properties and do not add notes "
                    "or alternate fields."
                ),
                "properties": {
                    "context_needs": {
                        "type": "array",
                        "maxItems": 2,
                        "description": (
                            "Historical lookups that may still be required after "
                            "the automatic recall results supplied to the Owner. "
                            "Never repeat an intent recall query here; add a need "
                            "only for a distinct post-recall search or read."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "tool": {
                                    "type": "string",
                                    "enum": [
                                        "memory_search",
                                        "episode_search",
                                        "episode_read",
                                        "thinking_search",
                                        "thinking_read",
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
                                        "past_reasoning",
                                    ],
                                },
                            },
                            "required": ["tool", "query", "evidence"],
                            "additionalProperties": False,
                        },
                    },
                    "mcp_servers": {
                        "type": "array",
                        "maxItems": 32,
                        "description": (
                            "MCP server groups the Owner is expected to need. "
                            "Unexpected later needs can use tool_enable."
                        ),
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 100,
                        },
                    },
                    "strategy": {
                        "type": "array",
                        "maxItems": 8,
                        "description": (
                            "Minimum ordered task-level decisions. Empty for a "
                            "silent close; exactly one interaction objective with "
                            "recall use and material factual boundaries for ordinary "
                            "visible chat; expand only for genuinely multi-stage work "
                            "or material branches. Do not duplicate the intent, "
                            "mention absent tools, prescribe concrete advice, or plan "
                            "wording, style, bubbles, timing, persona, or reaction "
                            "assets. For social chat include only the interaction "
                            "objective, recall use, and factual boundaries."
                        ),
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 300,
                        },
                    },
                    "completion_criteria": {
                        "type": "array",
                        "maxItems": 3,
                        "description": (
                            "Only outcomes needed to distinguish completion from "
                            "an intermediate action. Empty for silence and routine "
                            "social response."
                        ),
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 300,
                        },
                    },
                    "response_mode": {
                        "type": "string",
                        "enum": ["silent", "visible"],
                        "description": (
                            "silent only when no owner-visible move remains; visible "
                            "when the Owner must react, answer, ask, report, or "
                            "otherwise communicate."
                        ),
                    },
                },
                "required": [
                    "context_needs",
                    "mcp_servers",
                    "strategy",
                    "completion_criteria",
                    "response_mode",
                ],
                "additionalProperties": False,
            },
            "uncertainty": {
                "type": "array",
                "maxItems": 4,
                "items": {"type": "string"},
                "description": (
                    "Material ambiguity remaining after planning. Missing "
                    "recallable identity or background requires search."
                ),
            },
        },
        "required": [
            "version",
            "intent_units",
            "episode_actions",
            "episode_links",
            "handoff",
            "uncertainty",
        ],
        "additionalProperties": False,
    },
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
            "version": {"type": "integer", "enum": [2]},
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
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 120,
                        },
                        "description": (
                            "For search, one to three ranked narrow exact-word "
                            "expressions. Each item is an independent retrieval need; "
                            "a result may satisfy any one item, and order affects "
                            "ranking only. Put the concrete current subject first, "
                            "then broader context. Use a concrete named anchor or concise "
                            "subject-plus-history-facet anchor. Join parallel, "
                            "equally weighted exact keywords or aliases with `|` "
                            "without spaces; any one may satisfy the need and "
                            "additional matches only strengthen ranking. Use "
                            "separate items for different "
                            "retrieval needs. Empty only for skip."
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
) -> list[str]:
    """Normalize parallel OR expressions."""

    return [
        re.sub(r"\s*\|\s*", "|", query)
        for query in _strings(
            value,
            name,
            minimum=minimum,
            maximum=maximum,
            max_length=120,
        )
    ]


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


def parse_context_plan(
    text: str | dict[str, object],
    event_ids: list[str],
    candidates: list[dict[str, object]],
    turn_id: str,
    revision: int,
    available_mcp_servers: set[str] | None = None,
    recall_reuse_candidates: list[dict[str, object]] | None = None,
) -> ContextPlan:
    if isinstance(text, dict):
        value = text
    else:
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, TypeError) as error:
            raise ContextPlanError("invalid_json") from error
    if not isinstance(value, dict):
        raise ContextPlanError("invalid_top_level")
    if value.get("version") != 5:
        raise ContextPlanError("unsupported_version")
    if set(value) != {
        "version",
        "intent_units",
        "episode_actions",
        "episode_links",
        "handoff",
        "uncertainty",
    }:
        raise ContextPlanError("invalid_top_level")

    raw_units = value["intent_units"]
    if not isinstance(raw_units, list) or not 1 <= len(raw_units) <= 12:
        raise ContextPlanError("invalid_intent_units")
    expected_events = set(event_ids)
    covered_events: set[str] = set()
    unit_ids: set[str] = set()
    units: list[dict[str, object]] = []
    required_unit_keys = {
        "id",
        "event_ids",
        "text",
        "intent",
        "speech_act",
        "references",
        "recall_mode",
        "recall_queries",
        "recall_from_turn_id",
    }
    for raw in raw_units:
        if not isinstance(raw, dict) or set(raw) != required_unit_keys:
            raise ContextPlanError("invalid_intent_unit")
        unit_id = _text(raw["id"], "unit_id", 40)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", unit_id) or unit_id in unit_ids:
            raise ContextPlanError("invalid_unit_id")
        source_ids = _strings(
            raw["event_ids"],
            "unit_event_ids",
            minimum=1,
            maximum=len(event_ids),
            max_length=500,
        )
        if not set(source_ids) <= expected_events:
            raise ContextPlanError("unknown_event_id")
        unit_ids.add(unit_id)
        covered_events.update(source_ids)
        speech_act = raw["speech_act"]
        if not isinstance(speech_act, str) or speech_act not in SPEECH_ACTS:
            raise ContextPlanError("invalid_speech_act")
        recall_mode = raw["recall_mode"]
        recall_queries = _recall_queries(
            raw["recall_queries"], "unit_recall_queries", minimum=0
        )
        if not isinstance(raw["recall_from_turn_id"], str):
            raise ContextPlanError("invalid_recall_source")
        recall_from_turn_id = raw["recall_from_turn_id"].strip()
        if len(recall_from_turn_id) > 64:
            raise ContextPlanError("invalid_recall_source")
        if recall_mode == "search":
            if not recall_queries or recall_from_turn_id:
                raise ContextPlanError("invalid_recall_search")
            recall = {"mode": "search", "queries": recall_queries}
        elif recall_mode == "reuse":
            if recall_queries or not recall_from_turn_id:
                raise ContextPlanError("invalid_recall_reuse")
            recall = {
                "mode": "reuse",
                "from_turn_id": recall_from_turn_id,
            }
        else:
            raise ContextPlanError("invalid_recall_decision")
        unit = {
            "id": unit_id,
            "event_ids": source_ids,
            "text": _text(raw["text"], "unit_text", 2000),
            "intent": _text(raw["intent"], "unit_intent", 200),
            "speech_act": speech_act,
            "references": _strings(
                raw["references"],
                "unit_references",
                maximum=8,
                max_length=500,
            ),
            "recall": recall,
            "recall_queries": recall_queries,
            "recall_from_turn_id": recall_from_turn_id,
        }
        units.append(unit)
    if covered_events != expected_events:
        raise ContextPlanError("uncovered_event_ids")

    candidate_ids = {str(item["id"]) for item in candidates}
    raw_actions = value["episode_actions"]
    if not isinstance(raw_actions, list) or not 1 <= len(raw_actions) <= 12:
        raise ContextPlanError("invalid_episode_actions")
    raw_bindings = []
    for raw in raw_actions:
        if not isinstance(raw, dict):
            raise ContextPlanError("invalid_episode_action")
        allowed = {
            "action",
            "episode_ref",
            "title",
            "unit_ids",
            "topics",
            "entities",
            "open_loops",
            "salience",
        }
        if set(raw) - allowed:
            raise ContextPlanError("invalid_episode_action")
        action = raw.get("action")
        if action == "none":
            if "unit_ids" not in raw:
                raise ContextPlanError("invalid_episode_action")
            raw_bindings.append(
                {
                    "action": "none",
                    "unit_ids": raw["unit_ids"],
                }
            )
            continue
        if action not in {"continue", "new"} or "unit_ids" not in raw:
            raise ContextPlanError("invalid_episode_action")
        if action == "continue" and "episode_ref" not in raw:
            raise ContextPlanError("missing_continue_episode_ref")
        if action == "new" and "episode_ref" not in raw:
            raise ContextPlanError("missing_new_episode_ref")
        if action == "new" and "title" not in raw:
            raise ContextPlanError("missing_new_episode_title")
        raw_bindings.append(
            {
                "action": action,
                "episode_ref": raw["episode_ref"],
                "unit_ids": raw["unit_ids"],
                "title": raw.get("title", ""),
                "topics": raw.get("topics", []),
                "entities": raw.get("entities", []),
                "open_loops": raw.get("open_loops", []),
                "salience": raw.get("salience", 0.0),
                "relation": "primary",
            }
        )
    bound_units: set[str] = set()
    bindings: list[dict[str, object]] = []
    bindings_by_ref: dict[str, dict[str, object]] = {}
    for raw in raw_bindings:
        if raw.get("action") == "none":
            binding_units = _strings(
                raw["unit_ids"],
                "binding_unit_ids",
                minimum=1,
                maximum=len(unit_ids),
                max_length=40,
            )
            if not set(binding_units) <= unit_ids:
                raise ContextPlanError("unknown_binding_unit")
            if bound_units & set(binding_units):
                raise ContextPlanError("duplicate_binding_unit")
            bound_units.update(binding_units)
            bindings.append({"action": "none", "unit_ids": binding_units})
            continue
        if not isinstance(raw, dict) or set(raw) != {
            "action",
            "episode_ref",
            "title",
            "relation",
            "unit_ids",
            "topics",
            "entities",
            "open_loops",
            "salience",
        }:
            raise ContextPlanError("invalid_episode_binding")
        episode_ref = _text(raw["episode_ref"], "episode_ref", 200)
        action = str(raw["action"])
        is_new = action == "new"
        if is_new:
            if not re.fullmatch(NEW_EPISODE_REF_FORMAT, episode_ref):
                raise ContextPlanError("invalid_new_episode_ref")
        elif episode_ref.startswith("new:") or episode_ref not in candidate_ids:
            raise ContextPlanError("unknown_episode_ref")
        binding_units = _strings(
            raw["unit_ids"],
            "binding_unit_ids",
            minimum=1,
            maximum=len(unit_ids),
            max_length=40,
        )
        if not set(binding_units) <= unit_ids:
            raise ContextPlanError("unknown_binding_unit")
        if bound_units & set(binding_units):
            raise ContextPlanError("duplicate_binding_unit")
        relation = raw["relation"]
        if not isinstance(relation, str) or relation not in {"primary", "related"}:
            raise ContextPlanError("invalid_episode_relation")
        salience = raw["salience"]
        if (
            isinstance(salience, bool)
            or not isinstance(salience, (int, float))
            or not 0 <= float(salience) <= 1
        ):
            raise ContextPlanError("invalid_episode_salience")
        title = (
            _text(raw["title"], "episode_title", 200)
            if is_new
            else str(
                next(
                    (
                        item.get("title") or "Conversation"
                        for item in candidates
                        if str(item["id"]) == episode_ref
                    ),
                    "Conversation",
                )
            )
        )
        actual_id = (
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"momoi:episode:{turn_id}:{revision}:{episode_ref}",
            ).hex
            if is_new
            else episode_ref
        )
        bound_units.update(binding_units)
        open_loops = _strings(
            raw["open_loops"],
            "episode_open_loops",
            maximum=8,
            max_length=500,
        )
        topics = _strings(raw["topics"], "episode_topics", maximum=12, max_length=200)
        entities = _strings(
            raw["entities"], "episode_entities", maximum=20, max_length=200
        )
        if episode_ref in bindings_by_ref:
            raise ContextPlanError("duplicate_episode_ref")
        binding = {
            "action": action,
            "episode_id": actual_id,
            "is_new": is_new,
            "title": title,
            "relation": relation,
            "unit_ids": binding_units,
            "topics": topics,
            "entities": entities,
            "open_loops": open_loops,
            "salience": float(salience),
            "_ref": episode_ref,
        }
        bindings.append(binding)
        bindings_by_ref[episode_ref] = binding
    if bound_units != unit_ids:
        raise ContextPlanError("unbound_intent_units")

    reuse_by_turn = {
        str(item.get("turn_id") or ""): item
        for item in recall_reuse_candidates or []
        if isinstance(item, dict) and item.get("turn_id")
    }
    for unit in units:
        recall = unit["recall"]
        if not isinstance(recall, dict) or recall.get("mode") != "reuse":
            continue
        source_turn_id = str(recall.get("from_turn_id") or "")
        candidate = reuse_by_turn.get(source_turn_id)
        if candidate is None:
            raise ContextPlanError("unknown_recall_reuse_source")

    ref_to_id = {episode_id: episode_id for episode_id in candidate_ids}
    ref_to_id.update(
        {
            str(item["_ref"]): str(item["episode_id"])
            for item in bindings
            if "_ref" in item
        }
    )
    raw_links = value["episode_links"]
    if not isinstance(raw_links, list) or len(raw_links) > 20:
        raise ContextPlanError("invalid_episode_links")
    links: list[dict[str, str]] = []
    seen_links: set[tuple[str, str, str]] = set()
    seen_link_pairs: dict[tuple[str, str], str] = {}
    ordering_edges: list[tuple[str, str]] = []
    for raw in raw_links:
        if not isinstance(raw, dict) or set(raw) != {
            "from_episode_ref",
            "to_episode_ref",
            "kind",
        }:
            raise ContextPlanError("invalid_episode_link")
        source_ref = _text(raw["from_episode_ref"], "link_source", 200)
        target_ref = _text(raw["to_episode_ref"], "link_target", 200)
        kind = raw["kind"]
        if source_ref not in ref_to_id or target_ref not in ref_to_id:
            raise ContextPlanError("unknown_link_episode")
        if (
            source_ref not in bindings_by_ref
            or source_ref == target_ref
            or not isinstance(kind, str)
            or kind not in {"continues", "references", "supersedes"}
        ):
            raise ContextPlanError("invalid_episode_link")
        link = (ref_to_id[str(source_ref)], ref_to_id[str(target_ref)], str(kind))
        if link in seen_links:
            raise ContextPlanError("duplicate_episode_link")
        pair = (link[0], link[1])
        prior_kind = seen_link_pairs.get(pair)
        if prior_kind is not None and prior_kind != link[2]:
            raise ContextPlanError("conflicting_episode_link")
        seen_links.add(link)
        seen_link_pairs[pair] = link[2]
        if link[2] in {"continues", "supersedes"}:
            ordering_edges.append(pair)
        links.append(
            {
                "from_episode_id": link[0],
                "to_episode_id": link[1],
                "kind": link[2],
            }
        )
    if _has_directed_cycle(ordering_edges):
        raise ContextPlanError("cyclic_episode_link")
    for binding in bindings:
        binding.pop("_ref", None)
    raw_handoff = value["handoff"]
    required_handoff_keys = {
        "context_needs",
        "mcp_servers",
        "strategy",
        "completion_criteria",
        "response_mode",
    }
    if not isinstance(raw_handoff, dict) or set(raw_handoff) != required_handoff_keys:
        raise ContextPlanError("invalid_owner_handoff")
    context_needs = _parse_context_needs(
        raw_handoff["context_needs"],
        tools={
            "memory_search",
            "episode_search",
            "episode_read",
            "thinking_search",
            "thinking_read",
        },
        evidence={
            "exact_wording",
            "chronology",
            "unresolved_reference",
            "correction_evidence",
            "relevant_history",
            "past_reasoning",
        },
        error="invalid_context_need",
        thinking_requires_past_reasoning=True,
    )
    servers = _strings(
        raw_handoff["mcp_servers"],
        "mcp_servers",
        maximum=32,
        max_length=100,
    )
    if len(set(servers)) != len(servers):
        raise ContextPlanError("duplicate_mcp_server")
    if available_mcp_servers is not None and not set(servers) <= available_mcp_servers:
        raise ContextPlanError("unknown_mcp_server")
    response_mode = raw_handoff["response_mode"]
    if response_mode not in {"silent", "visible"}:
        raise ContextPlanError("invalid_response_mode")
    strategy = _strings(
        raw_handoff["strategy"],
        "strategy",
        maximum=8,
        max_length=300,
    )
    criteria = _strings(
        raw_handoff["completion_criteria"],
        "completion_criteria",
        maximum=3,
        max_length=300,
    )
    if response_mode == "silent" and (strategy or criteria):
        raise ContextPlanError("invalid_silent_strategy")
    if response_mode == "visible" and not strategy:
        raise ContextPlanError("missing_visible_strategy")
    return {
        "version": 5,
        "intent_units": units,
        "episode_actions": bindings,
        "episode_links": links,
        "owner_handoff": {
            "context": {"needs": context_needs},
            "mcp": {"servers": servers},
            "strategy": {
                "plan": strategy,
                "completion_criteria": criteria,
                "response_mode": str(response_mode),
            },
        },
        "uncertainty": _strings(
            value["uncertainty"],
            "uncertainty",
            maximum=4,
            max_length=500,
        ),
    }


def degraded_context_plan(
    owner_messages: list[dict[str, str]], reason: str
) -> ContextPlan:
    segments: list[tuple[list[str], str]] = []
    for message in owner_messages:
        parts = [
            part.strip()
            for part in re.split(r"(?:\n+|(?<=[。！？!?；;])\s*)", message["text"])
            if part.strip()
        ] or ["(non-text owner message)"]
        for part in parts:
            segments.append(([message["event_id"]], part))
    if len(segments) > 12:
        overflow = segments[11:]
        segments = [
            *segments[:11],
            (
                list(
                    dict.fromkeys(
                        event_id
                        for source_ids, _ in overflow
                        for event_id in source_ids
                    )
                ),
                " ".join(text for _, text in overflow),
            ),
        ]
    units: list[dict[str, object]] = []
    for source_ids, part in segments:
        units.append(
            {
                "id": f"u{len(units) + 1}",
                "event_ids": source_ids,
                "text": part[:2000],
                "intent": "degraded_message_segment",
                "speech_act": "unknown",
                "references": [],
                "recall_mode": "search",
                "recall_queries": [" ".join(part.split())[:120]],
                "recall_from_turn_id": "",
            }
        )
    return {
        "version": 5,
        "intent_units": units,
        "episode_actions": [
            {"action": "none", "unit_ids": [str(unit["id"])]} for unit in units
        ],
        "episode_links": [],
        "uncertainty": [
            f"Context planner protocol failed ({reason}); deterministic message "
            "segmentation and literal fallback recall are used."
        ],
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
        or value.get("version") != 2
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
        "version": 2,
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
        "version": 2,
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
