from __future__ import annotations

import copy

from ..search import search_alternatives


CURRENT_RETRIEVAL_VERSION = 6
READABLE_RETRIEVAL_VERSIONS = frozenset({4, 5, 6})


def _query(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        semantic = " ".join(str(value.get("semantic") or "").split())[:240]
        keywords = [
            " ".join(str(keyword).split())[:60]
            for keyword in value.get("keywords") or []
            if " ".join(str(keyword).split())
        ][:8]
    else:
        semantic = " ".join(str(value or "").split())[:240]
        keywords = list(search_alternatives(semantic[:120]))[:8]
    if not semantic:
        return None
    return {"semantic": semantic, "keywords": list(dict.fromkeys(keywords))}


def normalize_context_plan(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("context plan must be an object")
    plan = copy.deepcopy(value)
    units = plan.get("intent_units")
    if isinstance(units, list):
        for unit in units:
            if not isinstance(unit, dict):
                continue
            unit["recall_queries"] = [
                normalized
                for item in unit.get("recall_queries") or []
                if (normalized := _query(item)) is not None
            ]
    activity = plan.get("activity")
    if isinstance(activity, dict):
        activity["recall_queries"] = [
            normalized
            for item in activity.get("recall_queries") or []
            if (normalized := _query(item)) is not None
        ]
    return plan


def _plan_query_semantics(plan: dict[str, object]) -> list[str]:
    units = list(plan.get("intent_units") or [])
    activity = plan.get("activity")
    if isinstance(activity, dict):
        units.append(activity)
    return list(
        dict.fromkeys(
            str(query.get("semantic") or "")[:240]
            for unit in units
            if isinstance(unit, dict)
            for query in unit.get("recall_queries") or []
            if isinstance(query, dict) and query.get("semantic")
        )
    )


def normalize_context_retrieval(
    value: object,
    plan: dict[str, object],
) -> dict[str, object]:
    if value in ({}, None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("context retrieval must be an object")
    version = value.get("version")
    if version not in READABLE_RETRIEVAL_VERSIONS:
        raise ValueError(f"unsupported context retrieval version: {version}")
    retrieval = copy.deepcopy(value)
    retrieval["version"] = CURRENT_RETRIEVAL_VERSION
    for field in (
        "episodes",
        "recall_memories",
        "reflection_memories",
        "goals",
        "uncertainty",
    ):
        if not isinstance(retrieval.get(field), list):
            retrieval[field] = []
    stored = retrieval.get("effective_recall_queries")
    if isinstance(stored, list):
        effective = [
            semantic
            for item in stored
            if (normalized := _query(item)) is not None
            and (semantic := str(normalized["semantic"]))
        ]
    else:
        status = str(retrieval.get("query_recall") or "")
        effective = (
            _plan_query_semantics(plan)
            if "hits=" in status and "misses=" not in status
            else []
        )
    retrieval["effective_recall_queries"] = list(dict.fromkeys(effective))
    return retrieval
