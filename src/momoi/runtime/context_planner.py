import json
import logging
import re
import uuid

from ..contracts import ContextPlan
from ..logging_context import log_event


logger = logging.getLogger(__name__)


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
CONTEXT_PLAN_TOOL_SPEC: dict[str, object] = {
    "name": CONTEXT_PLAN_TOOL_NAME,
    "description": "Submit the complete context plan for the current owner input.",
    "input_schema": {
        "type": "object",
        "properties": {
            "version": {"type": "integer", "enum": [2]},
            "intent_units": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "event_ids": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                        },
                        "text": {"type": "string"},
                        "intent": {"type": "string"},
                        "speech_act": {
                            "type": "string",
                            "enum": sorted(SPEECH_ACTS - {"unknown"}),
                        },
                        "references": {
                            "type": "array",
                            "maxItems": 8,
                            "items": {"type": "string"},
                        },
                        "recall_queries": {
                            "type": "array",
                            "maxItems": 2,
                            "items": {
                                "type": "string",
                                "maxLength": 500,
                                "description": (
                                    "Planner-generated pipe-separated OR search "
                                    "terms: identifiers and useful alternative "
                                    "wording likely to occur in stored evidence."
                                ),
                            },
                        },
                    },
                    "required": [
                        "id",
                        "event_ids",
                        "text",
                        "intent",
                        "speech_act",
                        "references",
                        "recall_queries",
                    ],
                    "additionalProperties": False,
                },
            },
            "episode_actions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": ["none"]},
                                "unit_ids": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["action", "unit_ids"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["continue"],
                                },
                                "episode_ref": {
                                    "type": "string",
                                    "description": (
                                        "An existing candidate Episode id."
                                    ),
                                },
                                "unit_ids": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {"type": "string"},
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
                            "required": [
                                "action",
                                "episode_ref",
                                "unit_ids",
                                "topics",
                                "entities",
                                "open_loops",
                                "salience",
                            ],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": ["new"]},
                                "episode_ref": {
                                    "type": "string",
                                    "description": (
                                        "A new:<key> reference using a lowercase "
                                        "ASCII slug."
                                    ),
                                },
                                "title": {"type": "string"},
                                "unit_ids": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {"type": "string"},
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
                            "required": [
                                "action",
                                "episode_ref",
                                "title",
                                "unit_ids",
                                "topics",
                                "entities",
                                "open_loops",
                                "salience",
                            ],
                            "additionalProperties": False,
                        },
                    ]
                },
            },
            "episode_links": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "from_episode_ref": {"type": "string"},
                        "to_episode_ref": {
                            "type": "string",
                            "description": (
                                "A bound episode_ref or an existing candidate Episode "
                                "id. Existing link targets need not be bound to this Turn."
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
            "tool_groups": {
                "type": "array",
                "maxItems": 32,
                "items": {"type": "string", "minLength": 1, "maxLength": 100},
                "description": (
                    "Available tool-group ids needed to handle the current owner "
                    "input now. Use an empty array for ordinary conversation."
                ),
            },
            "uncertainty": {
                "type": "array",
                "maxItems": 4,
                "items": {"type": "string"},
            },
        },
        "required": [
            "version",
            "intent_units",
            "episode_actions",
            "episode_links",
            "tool_groups",
            "uncertainty",
        ],
        "additionalProperties": False,
    },
}
HEARTBEAT_PLAN_TOOL_NAME = "submit_heartbeat_plan"
HEARTBEAT_PLAN_TOOL_SPEC: dict[str, object] = {
    "name": HEARTBEAT_PLAN_TOOL_NAME,
    "description": (
        "Submit the selected heartbeat activity and its initial recall queries."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "version": {"type": "integer", "enum": [1]},
            "activity": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "maxLength": 300},
                    "reason": {"type": "string", "maxLength": 300},
                    "recall_queries": {
                        "type": "array",
                        "maxItems": 2,
                        "items": {
                            "type": "string",
                            "maxLength": 500,
                            "description": (
                                "Planner-generated pipe-separated OR search terms: "
                                "identifiers and useful alternative wording likely "
                                "to occur in stored evidence."
                            ),
                        },
                    },
                },
                "required": ["intent", "reason", "recall_queries"],
                "additionalProperties": False,
            },
            "uncertainty": {
                "type": "array",
                "maxItems": 4,
                "items": {"type": "string", "maxLength": 500},
            },
        },
        "required": ["version", "activity", "uncertainty"],
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
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise ContextPlanError(f"invalid_{name}")
    return value.strip()


def _merge_unique(
    target: list[str],
    incoming: list[str],
    *,
    maximum: int,
    error: str,
) -> None:
    target.extend(item for item in incoming if item not in target)
    if len(target) > maximum:
        raise ContextPlanError(error)


def parse_context_plan(
    text: str | dict[str, object],
    event_ids: list[str],
    candidates: list[dict[str, object]],
    turn_id: str,
    revision: int,
    available_tool_groups: set[str] | None = None,
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
    version = value.get("version")
    expected = {
        "version",
        "intent_units",
        "episode_actions" if version == 2 else "episode_bindings",
        "episode_links",
        "uncertainty",
    }
    if set(value) not in (expected, {*expected, "tool_groups"}):
        raise ContextPlanError("invalid_top_level")
    if version not in {1, 2}:
        raise ContextPlanError("unsupported_version")

    raw_units = value["intent_units"]
    if not isinstance(raw_units, list) or not 1 <= len(raw_units) <= 12:
        raise ContextPlanError("invalid_intent_units")
    expected_events = set(event_ids)
    covered_events: set[str] = set()
    unit_ids: set[str] = set()
    units: list[dict[str, object]] = []
    for raw in raw_units:
        legacy_keys = {
            "id",
            "event_ids",
            "text",
            "intent",
            "references",
            "recall_queries",
        }
        if not isinstance(raw, dict) or set(raw) not in (
            legacy_keys,
            {*legacy_keys, "speech_act"},
        ):
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
        speech_act = raw.get("speech_act", "unknown")
        if not isinstance(speech_act, str) or speech_act not in SPEECH_ACTS:
            raise ContextPlanError("invalid_speech_act")
        units.append(
            {
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
                "recall_queries": _strings(
                    raw["recall_queries"],
                    "unit_recall_queries",
                    minimum=0,
                    maximum=2,
                    max_length=500,
                ),
            }
        )
    if covered_events != expected_events:
        raise ContextPlanError("uncovered_event_ids")

    candidate_ids = {str(item["id"]) for item in candidates}
    if version == 2:
        raw_actions = value["episode_actions"]
        if not isinstance(raw_actions, list) or not 1 <= len(raw_actions) <= 12:
            raise ContextPlanError("invalid_episode_actions")
        raw_bindings = []
        for raw in raw_actions:
            if not isinstance(raw, dict):
                raise ContextPlanError("invalid_episode_action")
            action = raw.get("action")
            if action == "none":
                if set(raw) != {"action", "unit_ids"}:
                    raise ContextPlanError("invalid_episode_action")
                raw_bindings.append(
                    {
                        "action": "none",
                        "unit_ids": raw["unit_ids"],
                    }
                )
                continue
            required = {
                "action",
                "episode_ref",
                "unit_ids",
                "topics",
                "entities",
                "open_loops",
                "salience",
            }
            if action == "new":
                required.add("title")
            if set(raw) != required or action not in {"continue", "new"}:
                raise ContextPlanError("invalid_episode_action")
            raw_bindings.append(
                {
                    **raw,
                    "title": raw.get("title", ""),
                    "relation": "primary",
                }
            )
    else:
        raw_bindings = value["episode_bindings"]
    if not isinstance(raw_bindings, list) or not 1 <= len(raw_bindings) <= 12:
        raise ContextPlanError("invalid_episode_bindings")
    bound_units: set[str] = set()
    bindings: list[dict[str, object]] = []
    bindings_by_ref: dict[str, dict[str, object]] = {}
    merged_duplicates = 0
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
            *(["action"] if version == 2 else []),
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
        action = str(
            raw.get("action")
            or ("new" if episode_ref.startswith("new:") else "continue")
        )
        is_new = action == "new"
        if is_new:
            if not re.fullmatch(r"new:[a-z0-9][a-z0-9_-]{0,39}", episode_ref):
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
        if version == 2 and bound_units & set(binding_units):
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
            if is_new or version == 1
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
        topics = _strings(
            raw["topics"], "episode_topics", maximum=12, max_length=200
        )
        entities = _strings(
            raw["entities"], "episode_entities", maximum=20, max_length=200
        )
        existing = bindings_by_ref.get(episode_ref)
        if existing is None:
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
            continue

        if version == 2:
            raise ContextPlanError("duplicate_episode_ref")
        merged_duplicates += 1
        if existing["title"] != title:
            raise ContextPlanError("conflicting_episode_title")
        existing["relation"] = (
            "primary"
            if "primary" in {str(existing["relation"]), relation}
            else "related"
        )
        existing["salience"] = max(float(existing["salience"]), float(salience))
        _merge_unique(
            existing["unit_ids"],  # type: ignore[arg-type]
            binding_units,
            maximum=len(unit_ids),
            error="merged_episode_unit_ids_limit",
        )
        _merge_unique(
            existing["topics"],  # type: ignore[arg-type]
            topics,
            maximum=12,
            error="merged_episode_topics_limit",
        )
        _merge_unique(
            existing["entities"],  # type: ignore[arg-type]
            entities,
            maximum=20,
            error="merged_episode_entities_limit",
        )
        _merge_unique(
            existing["open_loops"],  # type: ignore[arg-type]
            open_loops,
            maximum=8,
            error="merged_episode_open_loops_limit",
        )
    if bound_units != unit_ids:
        raise ContextPlanError("unbound_intent_units")
    if version == 1 and not any(
        item.get("relation") == "primary" for item in bindings
    ):
        raise ContextPlanError("missing_primary_episode")

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
            source_ref == target_ref
            or not isinstance(kind, str)
            or kind not in {"continues", "references", "supersedes"}
        ):
            raise ContextPlanError("invalid_episode_link")
        link = (ref_to_id[str(source_ref)], ref_to_id[str(target_ref)], str(kind))
        if link in seen_links:
            raise ContextPlanError("duplicate_episode_link")
        seen_links.add(link)
        links.append(
            {
                "from_episode_id": link[0],
                "to_episode_id": link[1],
                "kind": link[2],
            }
        )
    if merged_duplicates:
        log_event(
            logger,
            logging.INFO,
            "context_plan_normalized",
            stage="context_plan",
            turn_id=turn_id,
            revision=revision,
            reason="duplicate_episode_ref",
            duplicates=merged_duplicates,
        )
    for binding in bindings:
        binding.pop("_ref", None)
    raw_tool_groups = value.get("tool_groups")
    tool_groups: list[str] | None = None
    if raw_tool_groups is not None:
        tool_groups = _strings(
            raw_tool_groups,
            "tool_groups",
            maximum=32,
            max_length=100,
        )
        if len(set(tool_groups)) != len(tool_groups):
            raise ContextPlanError("duplicate_tool_group")
        if available_tool_groups is not None and not set(tool_groups) <= available_tool_groups:
            raise ContextPlanError("unknown_tool_group")
    return {
        "version": version,
        "intent_units": units,
        **(
            {"episode_actions": bindings}
            if version == 2
            else {"episode_bindings": bindings}
        ),
        "episode_links": links,
        **({"tool_groups": tool_groups} if tool_groups is not None else {}),
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
                "recall_queries": [],
            }
        )
    return {
        "version": 2,
        "intent_units": units,
        "episode_actions": [
            {"action": "none", "unit_ids": [str(unit["id"])]}
            for unit in units
        ],
        "episode_links": [],
        "uncertainty": [
            f"Context planner protocol failed ({reason}); deterministic message "
            "segmentation is used without automatic historical recall."
        ],
    }


def parse_heartbeat_plan(
    value: str | dict[str, object],
) -> dict[str, object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError) as error:
            raise ContextPlanError("invalid_json") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "activity", "uncertainty"}
        or value.get("version") != 1
    ):
        raise ContextPlanError("invalid_heartbeat_plan")
    activity = value.get("activity")
    if (
        not isinstance(activity, dict)
        or set(activity) != {"intent", "reason", "recall_queries"}
    ):
        raise ContextPlanError("invalid_heartbeat_activity")
    return {
        "version": 1,
        "activity": {
            "intent": _text(activity["intent"], "heartbeat_intent", 300),
            "reason": _text(activity["reason"], "heartbeat_reason", 300),
            "recall_queries": _strings(
                activity["recall_queries"],
                "heartbeat_recall_queries",
                maximum=2,
                max_length=500,
            ),
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
        "version": 1,
        "activity": {
            "intent": activity.strip() or "spend time freely",
            "reason": f"Heartbeat planner failed ({reason}); continue current activity.",
            "recall_queries": [],
        },
        "uncertainty": [f"Heartbeat planner protocol failed: {reason}"],
    }
