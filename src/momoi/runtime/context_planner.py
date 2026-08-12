import json
import re
import uuid


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
NON_OPEN_LOOP_SPEECH_ACTS = {
    "emotional_share",
    "casual_share",
    "banter",
    "acknowledgment",
    "closing",
}


def is_light_social_plan(plan: dict[str, object]) -> bool:
    units = plan.get("intent_units")
    return bool(units) and all(
        isinstance(unit, dict)
        and unit.get("speech_act") in NON_OPEN_LOOP_SPEECH_ACTS
        and not unit.get("recall_queries")
        for unit in units
    )


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


def parse_context_plan(
    text: str,
    event_ids: list[str],
    candidates: list[dict[str, object]],
    turn_id: str,
    revision: int,
) -> dict[str, object]:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError) as error:
        raise ContextPlanError("invalid_json") from error
    if not isinstance(value, dict) or set(value) != {
        "version",
        "intent_units",
        "episode_bindings",
        "episode_links",
        "uncertainty",
    }:
        raise ContextPlanError("invalid_top_level")
    if value["version"] != 1:
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
                    maximum=6,
                    max_length=500,
                ),
            }
        )
    if covered_events != expected_events:
        raise ContextPlanError("uncovered_event_ids")

    candidate_ids = {str(item["id"]) for item in candidates}
    raw_bindings = value["episode_bindings"]
    if not isinstance(raw_bindings, list) or not 1 <= len(raw_bindings) <= 12:
        raise ContextPlanError("invalid_episode_bindings")
    refs: set[str] = set()
    bound_units: set[str] = set()
    bindings: list[dict[str, object]] = []
    for raw in raw_bindings:
        if not isinstance(raw, dict) or set(raw) != {
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
        is_new = episode_ref.startswith("new:")
        if is_new:
            if not re.fullmatch(r"new:[a-z0-9][a-z0-9_-]{0,39}", episode_ref):
                raise ContextPlanError("invalid_new_episode_ref")
        elif episode_ref not in candidate_ids:
            raise ContextPlanError("unknown_episode_ref")
        if episode_ref in refs:
            raise ContextPlanError("duplicate_episode_ref")
        refs.add(episode_ref)
        binding_units = _strings(
            raw["unit_ids"],
            "binding_unit_ids",
            minimum=1,
            maximum=len(unit_ids),
            max_length=40,
        )
        if not set(binding_units) <= unit_ids:
            raise ContextPlanError("unknown_binding_unit")
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
        bound_speech_acts = {
            str(unit["speech_act"])
            for unit in units
            if str(unit["id"]) in binding_units
        }
        if bound_speech_acts and bound_speech_acts <= NON_OPEN_LOOP_SPEECH_ACTS:
            open_loops = []
        bindings.append(
            {
                "episode_id": actual_id,
                "is_new": is_new,
                "title": _text(raw["title"], "episode_title", 200),
                "relation": relation,
                "unit_ids": binding_units,
                "topics": _strings(
                    raw["topics"], "episode_topics", maximum=12, max_length=200
                ),
                "entities": _strings(
                    raw["entities"], "episode_entities", maximum=20, max_length=200
                ),
                "open_loops": open_loops,
                "salience": float(salience),
                "_ref": episode_ref,
            }
        )
    if bound_units != unit_ids:
        raise ContextPlanError("unbound_intent_units")
    if not any(item["relation"] == "primary" for item in bindings):
        raise ContextPlanError("missing_primary_episode")

    ref_to_id = {str(item["_ref"]): str(item["episode_id"]) for item in bindings}
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
    for binding in bindings:
        binding.pop("_ref")
    return {
        "version": 1,
        "intent_units": units,
        "episode_bindings": bindings,
        "episode_links": links,
        "uncertainty": _strings(
            value["uncertainty"],
            "uncertainty",
            maximum=8,
            max_length=500,
        ),
    }


def degraded_context_plan(
    owner_messages: list[dict[str, str]], reason: str
) -> dict[str, object]:
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
                "recall_queries": [part[:500]],
            }
        )
    return {
        "version": 1,
        "intent_units": units,
        "episode_bindings": [],
        "episode_links": [],
        "uncertainty": [
            f"Context planner protocol failed ({reason}); recall uses deterministic "
            "message segmentation and may miss references or episode relations."
        ],
    }
