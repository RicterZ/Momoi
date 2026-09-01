from __future__ import annotations

import copy
import json
import logging
from typing import TYPE_CHECKING

from ..config import AppConfig
from ..context_time import context_timestamp
from ..logging_context import log_event, safe_preview
from ..search import search_alternatives
from ..storage import (
    REFLECTION_MEMORY_CAUTION,
    MemoryRecallQuery,
    Store,
    estimate_tokens,
    format_reflection_memory,
    truncate_tokens,
)
from ..storage.episode_ranking import EpisodeRecallQuery, rank_recall_items
from .budget import SECTION_BUDGET_ALLOCATOR

if TYPE_CHECKING:
    from ..semantic import DenseRecallEvidence


logger = logging.getLogger(__name__)
PLAN_RECALL_QUERY_LIMIT = 6
RECENT_EXTERNAL_EVENT_LIMIT = 6
RECENT_EXTERNAL_EVENT_LOOKBACK_SECONDS = 6 * 3600
RECENT_EXTERNAL_EVENT_TOKEN_BUDGET = 1200


def recall_query_semantic(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("semantic")
    return " ".join(str(value or "").split())[:240]


def select_plan_recall_queries(
    plan: dict[str, object],
) -> tuple[list[dict[str, object]], dict[str, list[str]], set[str], set[str]]:
    """Fan out structured recall while keeping sparse and dense inputs separate."""
    recall_units = list(plan.get("intent_units") or [])
    activity = plan.get("activity")
    if isinstance(activity, dict) and activity.get("recall_queries"):
        recall_units.append(
            {
                "id": "heartbeat_activity",
                "intent": activity.get("intent"),
                "recall_queries": activity["recall_queries"],
            }
        )
    reused_units_by_turn: dict[str, list[str]] = {}
    unit_queries: list[tuple[str, list[dict[str, object]]]] = []
    for unit in recall_units:
        if not isinstance(unit, dict):
            continue
        unit_id = str(unit.get("id") or "")
        recall = unit.get("recall")
        if isinstance(recall, dict) and recall.get("mode") == "reuse":
            source_turn_id = str(recall.get("from_turn_id") or "")
            if source_turn_id and unit_id:
                reused_units_by_turn.setdefault(source_turn_id, []).append(unit_id)
            continue
        queries: list[dict[str, object]] = []
        for raw_query in unit.get("recall_queries") or []:
            if isinstance(raw_query, dict):
                semantic = " ".join(str(raw_query.get("semantic") or "").split())[:240]
                raw_keywords = raw_query.get("keywords") or []
                keywords = [
                    " ".join(str(keyword).split())[:60]
                    for keyword in raw_keywords
                    if " ".join(str(keyword).split())
                ]
            else:
                # Historical v5 plans stored one expression for both channels.
                legacy = " ".join(str(raw_query).split())[:120]
                semantic = legacy
                keywords = list(search_alternatives(legacy))
            query = {
                "semantic": semantic,
                "keywords": list(dict.fromkeys(keywords)),
            }
            if semantic and query not in queries:
                queries.append(query)
        if queries:
            unit_queries.append((unit_id, queries))
    emitted_queries = {
        str(query["semantic"])
        for _unit_id, queries in unit_queries
        for query in queries
    }
    selected_by_query: dict[str, dict[str, object]] = {}
    selected: list[dict[str, object]] = []
    skipped_unit_ids: set[str] = set()
    for query_rank in range(max((len(queries) for _, queries in unit_queries), default=0)):
        for unit_id, queries in unit_queries:
            if query_rank >= len(queries):
                continue
            query = queries[query_rank]
            semantic = str(query["semantic"])
            keywords = list(query["keywords"])
            existing = selected_by_query.get(semantic)
            if existing is not None:
                unit_ids = existing["unit_ids"]
                assert isinstance(unit_ids, list)
                if unit_id and unit_id not in unit_ids:
                    unit_ids.append(unit_id)
                existing["priority"] = min(int(existing["priority"]), query_rank)
                existing_keywords = existing["keywords"]
                assert isinstance(existing_keywords, list)
                existing_keywords[:] = list(
                    dict.fromkeys([*existing_keywords, *keywords])
                )[:8]
                existing["expression"] = "|".join(existing_keywords)
                continue
            if len(selected) >= PLAN_RECALL_QUERY_LIMIT:
                if unit_id:
                    skipped_unit_ids.add(unit_id)
                continue
            item = {
                "expression": "|".join(keywords),
                "semantic_expression": semantic,
                "keywords": keywords,
                "unit_ids": [unit_id] if unit_id else [],
                "priority": query_rank,
            }
            selected_by_query[semantic] = item
            selected.append(item)
    return selected, reused_units_by_turn, emitted_queries, skipped_unit_ids


def _recall_log_text(value: object, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."


def _recall_log_matches(value: object, limit: int = 2) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    results: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "turn_id": item.get("turn_id"),
                "role": item.get("role"),
                "timestamp": item.get("timestamp"),
                "content": _recall_log_text(item.get("content")),
            }
        )
        if len(results) >= limit:
            break
    return results


def _merge_matches(target: dict[str, object], source: dict[str, object]) -> None:
    existing = target.get("matches")
    incoming = source.get("matches")
    if not isinstance(existing, list) or not isinstance(incoming, list):
        return
    seen = {item.get("id") for item in existing if isinstance(item, dict)}
    for item in incoming:
        if isinstance(item, dict) and item.get("id") not in seen:
            existing.append(item)
            seen.add(item.get("id"))


def build_plan_retrieval(
    store: Store,
    plan: dict[str, object],
    config: AppConfig,
    dense_evidence: DenseRecallEvidence | None = None,
) -> dict[str, object]:
    recent_episode_ids: set[str] = set()
    episodes: list[dict[str, object]] = []
    goals = [
        {
            name: row.get(name)
            for name in (
                "id",
                "status",
                "title",
                "next_action",
                "waiting_for",
                "blocked_reason",
                "latest_result",
                "next_review_at",
                "next_review_timestamp",
                "retry_at",
                "retry_timestamp",
                "schedule",
            )
        }
        for row in store.list_goals()[
            : config.policies.context.max_visible_goals
        ]
    ]
    # Every search-mode unit carries one or more recall expressions. Execute
    # them fairly across units and share the exact selection with dense recall.
    (
        recall_queries,
        reused_units_by_turn,
        emitted_queries,
        skipped_unit_ids,
    ) = select_plan_recall_queries(plan)

    inherited_memories: list[dict[str, object]] = []
    inherited_reflections: list[dict[str, object]] = []
    inherited_episodes: list[dict[str, object]] = []
    inherited_queries: list[str] = []
    visited_reuse_sources: set[str] = set()

    def inherit_recall(source_turn_id: str, unit_ids: list[str]) -> None:
        if not source_turn_id or source_turn_id in visited_reuse_sources:
            return
        visited_reuse_sources.add(source_turn_id)
        record = store.context_plan(source_turn_id)
        if record is None or record.get("state") != "recalled":
            return
        source_retrieval = record.get("retrieval")
        source_plan = record.get("plan")
        if (
            not isinstance(source_retrieval, dict)
            or source_retrieval.get("version") not in {4, 5, 6}
            or not isinstance(source_plan, dict)
        ):
            return
        stored_queries = source_retrieval.get("effective_recall_queries")
        if isinstance(stored_queries, list) and stored_queries:
            inherited_queries.extend(
                recall_query_semantic(query)
                for query in stored_queries
                if recall_query_semantic(query)
            )
        else:
            query_recall = str(source_retrieval.get("query_recall") or "")
            if "hits=" in query_recall and "misses=" not in query_recall:
                inherited_queries.extend(
                    recall_query_semantic(query)
                    for source_unit in source_plan.get("intent_units") or []
                    if isinstance(source_unit, dict)
                    for query in source_unit.get("recall_queries") or []
                    if recall_query_semantic(query)
                )
        for item in source_retrieval.get("recall_memories") or []:
            if isinstance(item, dict):
                inherited_memories.append(
                    {**copy.deepcopy(item), "unit_ids": unit_ids}
                )
        for item in source_retrieval.get("reflection_memories") or []:
            if isinstance(item, dict):
                inherited_reflections.append(
                    {**copy.deepcopy(item), "unit_ids": unit_ids}
                )
        for item in source_retrieval.get("episodes") or []:
            if not isinstance(item, dict) or not item.get("matched_queries"):
                continue
            inherited = copy.deepcopy(item)
            inherited["unit_ids"] = unit_ids
            for query in inherited.get("matched_queries") or []:
                if isinstance(query, dict):
                    query["unit_ids"] = unit_ids
            inherited_episodes.append(inherited)
        if isinstance(stored_queries, list) and stored_queries:
            return
        for source_unit in source_plan.get("intent_units") or []:
            if not isinstance(source_unit, dict):
                continue
            parent_turn_id = str(source_unit.get("recall_from_turn_id") or "")
            if parent_turn_id:
                inherit_recall(parent_turn_id, unit_ids)

    for source_turn_id, unit_ids in reused_units_by_turn.items():
        inherit_recall(source_turn_id, list(dict.fromkeys(unit_ids)))

    episode_queries = [
        EpisodeRecallQuery(
            expression=str(item["expression"]),
            unit_ids=tuple(str(value) for value in item["unit_ids"]),
            priority=int(item["priority"]),
            semantic_expression=str(item["semantic_expression"]),
        )
        for item in recall_queries
    ]
    episode_rows = store.search_episode_queries(
        episode_queries,
        max(0, config.summary_results),
        dense_evidence=dense_evidence,
    )
    episode_hit_query_set = {
        str(query.get("expression") or "")
        for row in episode_rows
        for query in row.get("matched_queries") or []
        if isinstance(query, dict) and query.get("expression")
    }
    ranked_memories = store.rank_recalled_memories(
        [
            MemoryRecallQuery(
                expression=str(item["expression"]),
                unit_ids=tuple(str(value) for value in item["unit_ids"]),
                priority=int(item["priority"]),
                semantic_expression=str(item["semantic_expression"]),
            )
            for item in recall_queries
        ],
        max(0, config.memory_results),
        dense_evidence=dense_evidence,
    )
    recall_memories = [
        {
            "kind": truncate_tokens(str(row.get("kind") or ""), 24),
            "key": truncate_tokens(str(row.get("key") or ""), 64),
            "content": str(row.get("content") or ""),
            "unit_ids": list(row.get("unit_ids") or []),
        }
        for row in ranked_memories
        if row.get("source") == "confirmed"
    ]
    reflection_memories = [
        {
            "kind": truncate_tokens(str(row.get("kind") or ""), 24),
            "key": truncate_tokens(str(row.get("key") or ""), 64),
            "content": str(row.get("content") or ""),
            "local_date": str(row.get("local_date") or "unknown"),
            "unit_ids": list(row.get("unit_ids") or []),
        }
        for row in ranked_memories
        if row.get("source") == "reflection"
    ]
    for target, inherited in (
        (recall_memories, inherited_memories),
        (reflection_memories, inherited_reflections),
    ):
        seen = {
            (
                str(item.get("kind") or ""),
                str(item.get("key") or ""),
                str(item.get("content") or ""),
            )
            for item in target
        }
        for item in inherited:
            if len(target) >= config.memory_results:
                break
            identity = (
                str(item.get("kind") or ""),
                str(item.get("key") or ""),
                str(item.get("content") or ""),
            )
            if identity in seen:
                continue
            target.append(item)
            seen.add(identity)
    memory_hit_query_set = {
        str(query)
        for row in ranked_memories
        if row.get("source") == "confirmed"
        for query in row.get("matched_queries") or []
    }
    reflection_hit_query_set = {
        str(query)
        for row in ranked_memories
        if row.get("source") == "reflection"
        for query in row.get("matched_queries") or []
    }
    recalled_episode_rows: dict[str, dict[str, object]] = {}
    recall_hits: list[str] = []
    recall_misses: list[str] = []
    memory_hit_queries: list[str] = []
    reflection_hit_queries: list[str] = []
    episode_hit_queries: list[str] = []
    for recall_query in recall_queries:
        query = str(recall_query["semantic_expression"])
        memory_hit = query in memory_hit_query_set
        reflection_hit = query in reflection_hit_query_set
        episode_hit = query in episode_hit_query_set
        query_hit = memory_hit or reflection_hit or episode_hit
        if memory_hit:
            memory_hit_queries.append(query)
        if reflection_hit:
            reflection_hit_queries.append(query)
        if episode_hit:
            episode_hit_queries.append(query)
        (recall_hits if query_hit else recall_misses).append(query)

    for row in episode_rows:
        episode_id = str(row.get("id") or "")
        if not episode_id:
            continue
        matched_queries = [
            item
            for item in row.get("matched_queries") or []
            if isinstance(item, dict)
        ]
        unit_ids = sorted(
            {
                str(unit_id)
                for item in matched_queries
                for unit_id in item.get("unit_ids") or []
            }
        )
        recalled_episode_rows[episode_id] = {
            "episode_id": episode_id,
            "relation": "recalled",
            "is_new": False,
            "matches": list(row.get("matches") or []),
            "unit_ids": unit_ids,
            "last_activity_at": float(row.get("last_activity_at") or 0),
            "salience": float(row.get("salience") or 0),
            "matched_keywords": list(row.get("matched_keywords") or []),
            "keyword_match_count": int(row.get("keyword_match_count") or 0),
            "search_score": float(row.get("search_score") or 0),
            "semantic_score": float(row.get("semantic_score") or 0),
            "matched_queries": matched_queries,
            "is_recent": episode_id in recent_episode_ids,
        }

    for inherited in inherited_episodes:
        episode_id = str(inherited.get("episode_id") or "")
        if not episode_id:
            continue
        existing = recalled_episode_rows.get(episode_id)
        if existing is None:
            inherited["is_recent"] = episode_id in recent_episode_ids
            recalled_episode_rows[episode_id] = inherited
            continue
        _merge_matches(existing, inherited)
        existing["unit_ids"] = sorted(
            {
                *(str(item) for item in existing.get("unit_ids") or []),
                *(str(item) for item in inherited.get("unit_ids") or []),
            }
        )

    # Only query-specific or explicitly reused Episodes enter recalled context.
    ranked_recalled_episodes = rank_recall_items(
        list(recalled_episode_rows.values())
    )
    existing_episodes = {
        str(item.get("episode_id")): item for item in episodes
    }
    for selected in ranked_recalled_episodes:
        episode_id = str(selected["episode_id"])
        existing = existing_episodes.get(episode_id)
        if existing is None:
            episodes.append(selected)
            existing_episodes[episode_id] = selected
            continue
        _merge_matches(existing, selected)
        existing["unit_ids"] = sorted(
            {
                *(str(item) for item in existing.get("unit_ids") or []),
                *(str(item) for item in selected.get("unit_ids") or []),
            }
        )
        existing["matched_keywords"] = list(
            selected.get("matched_keywords") or []
        )
        existing["keyword_match_count"] = int(
            selected.get("keyword_match_count") or 0
        )
        existing["search_score"] = float(
            selected.get("search_score") or 0
        )
        existing["semantic_score"] = float(
            selected.get("semantic_score") or 0
        )
        existing["matched_queries"] = list(
            selected.get("matched_queries") or []
        )
        existing["is_recent"] = True
    episodes = rank_recall_items(episodes)
    recall_index: list[str] = []
    if reused_units_by_turn:
        recall_index.extend(
            f"reused_from={turn_id} units={','.join(dict.fromkeys(unit_ids))}"
            for turn_id, unit_ids in reused_units_by_turn.items()
        )
    if recall_queries:
        recall_index.append(
            "semantic_queries="
            + " | ".join(
                str(item["semantic_expression"]) for item in recall_queries
            )
        )
        recall_index.append(
            "sparse_keywords="
            + " ; ".join(
                ",".join(str(keyword) for keyword in item["keywords"])
                or "(none)"
                for item in recall_queries
            )
        )
        recall_index.append(
            f"query_count={len(recall_queries)}/{len(emitted_queries)}"
        )
        if skipped_unit_ids:
            recall_index.append("skipped_units=" + ",".join(sorted(skipped_unit_ids)))
        if recall_hits:
            recall_index.append("hits=" + ",".join(recall_hits))
        if recall_misses:
            recall_index.append("misses=" + " | ".join(recall_misses))
        if memory_hit_queries:
            recall_index.append(
                "memory_hits=" + " | ".join(memory_hit_queries)
            )
        if reflection_hit_queries:
            recall_index.append(
                "reflection_hits=" + " | ".join(reflection_hit_queries)
            )
        if episode_hit_queries:
            recall_index.append(
                "episode_hits=" + " | ".join(episode_hit_queries)
            )
    effective_recall_queries = list(
        dict.fromkeys([*recall_hits, *inherited_queries])
    )
    retrieval = {
        "version": 6,
        "episodes": episodes,
        "long_term_memories": store.always_memory_context(),
        "recent_memories": store.recent_memory_context(),
        "recall_memories": recall_memories,
        "reflection_memories": reflection_memories,
        "goals": goals,
        "uncertainty": plan.get("uncertainty", []),
        "query_recall": "\n".join(recall_index),
        "effective_recall_queries": effective_recall_queries,
        "semantic_recall": {
            "space_id": dense_evidence.space_id if dense_evidence else "",
            "profile": dense_evidence.calibration_profile if dense_evidence else "",
            "query_batch_size": dense_evidence.query_batch_size if dense_evidence else 0,
            "request_ms": dense_evidence.request_ms if dense_evidence else 0.0,
            "search_ms": dense_evidence.search_ms if dense_evidence else 0.0,
            "fallback_reason": dense_evidence.fallback_reason if dense_evidence else "disabled",
        },
    }
    query_log = [
        {
            "expression": item["expression"],
            "semantic_expression": item["semantic_expression"],
            "keywords": item["keywords"],
            "priority": item["priority"],
            "unit_ids": item["unit_ids"],
        }
        for item in recall_queries
    ]
    memory_log = [
        {
            "source": row.get("source"),
            "kind": row.get("kind"),
            "key": row.get("key"),
            "score": round(float(row.get("search_score") or 0.0), 4),
            "floor": row.get("score_floor"),
            "queries": row.get("matched_queries"),
            "channels": row.get("channels") or [],
            "cosine": row.get("dense_cosine"),
            "agreement_bonus": row.get("agreement_bonus"),
            "dense_only": row.get("dense_only"),
            "content": _recall_log_text(row.get("content")),
        }
        for row in ranked_memories
    ]
    episode_log = [
        {
            "episode_id": row.get("id"),
            "title": _recall_log_text(row.get("title"), 160),
            "score": round(float(row.get("search_score") or 0.0), 4),
            "queries": [
                item.get("expression")
                for item in row.get("matched_queries") or []
                if isinstance(item, dict)
            ],
            "matched_keywords": row.get("matched_keywords") or [],
            "channels": row.get("channels") or [],
            "cosine": row.get("dense_cosine"),
            "agreement_bonus": row.get("agreement_bonus"),
            "corroboration_bonus": row.get("corroboration_bonus"),
            "dense_only": row.get("dense_only"),
            "evidence": _recall_log_matches(row.get("matches")),
        }
        for row in episode_rows
    ]
    log_event(
        logger,
        logging.INFO,
        "context_recall",
        stage="context_recall",
        queries=query_log,
        requested_query_count=len(emitted_queries),
        selected_query_count=len(recall_queries),
        skipped_unit_ids=sorted(skipped_unit_ids),
        reused_from_turn_ids=list(reused_units_by_turn),
        hits=recall_hits,
        misses=recall_misses,
        counts={
            "episodes": len(episodes),
            "goals": len(goals),
            "recall_queries": len(recall_queries),
            "recall_queries_emitted": len(emitted_queries),
            "recall_query_units_skipped": len(skipped_unit_ids),
            "recall_reuse_units": sum(
                len(set(unit_ids)) for unit_ids in reused_units_by_turn.values()
            ),
            "recall_reuse_sources": len(reused_units_by_turn),
            "recall_memory_hits": len(recall_memories),
            "recall_reflection_hits": len(reflection_memories),
            "recall_episode_hits": len(ranked_recalled_episodes),
        },
        embedding_space=dense_evidence.space_id if dense_evidence else "",
        embedding_profile=dense_evidence.calibration_profile if dense_evidence else "",
        embedding_query_batch_size=dense_evidence.query_batch_size if dense_evidence else 0,
        embedding_request_ms=round(dense_evidence.request_ms, 2) if dense_evidence else 0,
        embedding_search_ms=round(dense_evidence.search_ms, 2) if dense_evidence else 0,
        embedding_fallback=dense_evidence.fallback_reason if dense_evidence else "disabled",
    )
    log_event(
        logger,
        logging.INFO,
        "context_recall_memory_results",
        stage="context_recall",
        results=memory_log,
    )
    log_event(
        logger,
        logging.INFO,
        "context_recall_episode_results",
        stage="context_recall",
        results=episode_log,
    )
    log_event(
        logger,
        logging.INFO,
        "context_recall_state_results",
        stage="context_recall",
        goals=[
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "title": _recall_log_text(item.get("title"), 160),
                "next_action": _recall_log_text(item.get("next_action"), 240),
            }
            for item in goals
        ],
    )
    log_event(
        logger,
        logging.DEBUG,
        "context_recall_detail",
        stage="context_recall",
        selected=safe_preview(retrieval, 5000),
        memory_rank=safe_preview(
            [
                {
                    "source": row.get("source"),
                    "key": row.get("key"),
                    "score": round(float(row.get("search_score") or 0.0), 4),
                    "floor": row.get("score_floor"),
                    "queries": row.get("matched_queries"),
                }
                for row in ranked_memories
            ],
            3000,
        ),
    )
    return retrieval


def _supports(item: dict[str, object]) -> str:
    return ",".join(str(value) for value in item.get("unit_ids", []))


def _memory_lines(items: object) -> str:
    if not isinstance(items, list):
        return ""
    return "\n".join(
        f"- [{item['kind']}:{item['key']}] {item['content']}"
        for item in items
        if isinstance(item, dict)
        and item.get("kind") not in (None, "")
        and item.get("key") not in (None, "")
        and item.get("content") not in (None, "")
    )


def _reflection_memory_lines(items: object) -> str:
    if not isinstance(items, list):
        return ""
    return "\n".join(
        format_reflection_memory(item)
        for item in items
        if isinstance(item, dict)
        and item.get("kind") not in (None, "")
        and item.get("key") not in (None, "")
        and item.get("content") not in (None, "")
    )


def _episode_search_text(episode: dict[str, object]) -> str:
    return " ".join(
        str(episode.get(name) or "")
        for name in (
            "title",
            "narrative_summary",
            "working_summary",
            "topics",
            "entities",
            "open_loops",
            "matches",
        )
    )


_GOAL_PROGRESS_FIELDS = (
    ("next_action", "next", 100),
    ("waiting_for", "waiting", 80),
    ("latest_result", "last", 100),
    ("blocked_reason", "blocked", 80),
)


def _goal_directory_lines(items: object) -> str:
    """Render the part of a Goal that survives its execution unchanged."""

    if not isinstance(items, list):
        return ""
    return "\n".join(
        f"- id={item['id']} title={truncate_tokens(str(item.get('title') or ''), 80)}"
        for item in items
        if isinstance(item, dict) and item.get("id")
    )


def _goal_progress_lines(items: object) -> str:
    """Render the part of a Goal that changes as work happens."""

    if not isinstance(items, list):
        return ""
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        fields = [f"id={item['id']}", f"status={item.get('status') or 'unknown'}"]
        for key, label, limit in _GOAL_PROGRESS_FIELDS:
            value = item.get(key)
            if value not in (None, "", [], {}):
                fields.append(f"{label}={truncate_tokens(str(value), limit)}")
        lines.append("- " + " ".join(fields))
    return "\n".join(lines)


def _episode_summary(episode: dict[str, object]) -> tuple[str, str]:
    narrative = str(episode.get("narrative_summary") or "")
    if narrative:
        return narrative, "narrative"
    claims = episode.get("working_summary_claims")
    if isinstance(claims, list) and claims:
        return str(episode.get("working_summary") or ""), "extractive"
    return "", "empty"


def _episode_header(episode: dict[str, object], selected: dict[str, object]) -> str:
    parts = [f"id={episode['id']}"]
    units = _supports(selected)
    if units:
        parts.append(f"units={units}")
    relation = str(selected.get("relation") or "")
    if relation and relation != "recent":
        parts.append(f"relation={relation}")
    status = str(episode.get("status") or "")
    if status and status != "open":
        parts.append(f"status={status}")
    return f"[episode {' '.join(parts)}]"


def _episode_match_lines(
    selected: dict[str, object],
    token_budget: int,
    exclude_message_ids: set[int],
) -> list[str]:
    matches = [
        match
        for match in selected.get("matches") or []
        if isinstance(match, dict)
        and match.get("id") not in exclude_message_ids
        and str(match.get("content") or "").strip()
    ][:3]
    if not matches or token_budget <= 0:
        return []
    per_match = max(1, token_budget // len(matches))
    lines = ["matched_evidence:"]
    for match in matches:
        role = str(match.get("role") or "")
        delivery = str(match.get("delivery_state") or "")
        if role == "user":
            source = "OWNER"
        elif role == "assistant":
            source = f"MOMOI delivery={delivery or 'unknown'}"
        else:
            source = role.upper() or "UNKNOWN"
        lines.append(
            f"- [{source} timestamp={match.get('timestamp') or '?'} "
            f"turn={match.get('turn_id') or '?'}] "
            f"{truncate_tokens(str(match['content']), per_match)}"
        )
    return lines


def _episode_context(
    store: Store,
    episodes: object,
    summary_token_budget: int,
    raw_token_budget: int = 0,
    exclude_message_ids: set[int] | None = None,
    skip_empty_webhook: bool = False,
) -> str:
    if not isinstance(episodes, list):
        return ""
    existing = [
        item
        for item in episodes
        if not item.get("is_new") and store.episode(str(item["episode_id"]))
    ]
    if not existing or summary_token_budget <= 0:
        return ""
    per_summary = max(1, summary_token_budget // len(existing))
    per_raw = max(
        1,
        min(
            per_summary,
            (raw_token_budget or summary_token_budget) // len(existing),
        )
        // 2,
    )
    excluded = exclude_message_ids or set()
    sections: list[str] = []
    quality_counts: dict[str, int] = {}
    for selected in existing:
        episode = store.episode(str(selected["episode_id"]))
        if episode is None:
            continue
        if (
            skip_empty_webhook
            and str(episode.get("title") or "").startswith("Webhook event-message")
            and not _episode_summary(episode)[0]
        ):
            continue
        lines = [
            _episode_header(episode, selected),
            f"title: {episode['title']}",
        ]
        summary, quality = _episode_summary(episode)
        quality_counts[quality] = quality_counts.get(quality, 0) + 1
        lines.append(f"summary_quality: {quality}")
        lines.extend(_episode_match_lines(selected, per_raw, excluded))
        if summary:
            lines.append(
                f"summary: {truncate_tokens(summary, max(1, per_summary - per_raw))}"
            )
        if episode["topics"]:
            lines.append(f"topics: {json.dumps(episode['topics'], ensure_ascii=False)}")
        if episode["open_loops"]:
            lines.append(
                f"open_loops: {json.dumps(episode['open_loops'], ensure_ascii=False)}"
            )
        sections.append(truncate_tokens("\n".join(lines), per_summary))
    rendered = "\n\n".join(sections)
    log_event(
        logger,
        logging.INFO,
        "episode_directory_assembled",
        stage="context_recall",
        episodes=len(sections),
        tokens=estimate_tokens(rendered) if rendered else 0,
        raw_messages=0,
        summary_quality=quality_counts,
    )
    return rendered


def assemble_recent_external_events(
    store: Store,
    before_timestamp: float | None = None,
    *,
    limit: int = RECENT_EXTERNAL_EVENT_LIMIT,
    lookback_seconds: float = RECENT_EXTERNAL_EVENT_LOOKBACK_SECONDS,
    token_budget: int = RECENT_EXTERNAL_EVENT_TOKEN_BUDGET,
) -> str:
    """Render silent autonomous Events as a folded, low-priority ledger."""

    events = store.recent_external_events(limit, lookback_seconds, before_timestamp)
    blocks: list[str] = []
    for index, event in enumerate(events, 1):
        first_seen = float(event["first_seen"])
        last_seen = float(event["last_seen"])
        occurrences = int(event["occurrences"])
        lines = [
            f"E-{index} {context_timestamp(last_seen)} [{event['source']}]",
            f"  event: {event['event']}",
        ]
        if occurrences > 1:
            lines.append(
                f"  observations: {occurrences} since {context_timestamp(first_seen)}"
            )
        blocks.append("\n".join(lines))
    rendered = "\n\n".join(blocks)
    return truncate_tokens(rendered, max(1, token_budget)) if rendered else ""


def assemble_recent_webhook_activity(
    store: Store,
    turn_limit: int = 4,
    token_budget: int = 700,
) -> str:
    """Render a tiny ledger of completed webhook work for continuity.

    Keep tool names and outcome summaries, never raw tool payloads. This lets a
    later webhook avoid repeating an already completed notification without
    carrying the full prior tool transcript.
    """
    rows: list[str] = []
    for record in reversed(store.recent_turn_records(max(1, turn_limit * 3))):
        turn_id = str(record.get("turn_id") or "")
        if not turn_id.startswith("webhook:"):
            continue
        timeline = record.get("timeline")
        if not isinstance(timeline, list):
            continue
        calls: list[str] = []
        result_text = ""
        notified = False
        at = str(record.get("completed_at") or record.get("started_at") or "")
        for item in timeline:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            if kind == "tool_call":
                name = str(item.get("name") or "tool")
                calls.append(name)
            elif kind == "tool_result":
                if str(item.get("name") or "") == "send_bubbles":
                    notified = bool(item.get("ok", True))
                summary = item.get("summary") or item.get("result") or item.get("error")
                if summary and not result_text:
                    result_text = truncate_tokens(str(summary), 100)
        if not calls and not result_text:
            continue
        rows.append(
            f"{at} tool={', '.join(dict.fromkeys(calls)) or 'none'} "
            f"notification={'sent' if notified else 'not-sent'} "
            f"result={result_text or 'no summary'}"
        )
        if len(rows) >= turn_limit:
            break
    if not rows:
        return "(none)"
    return truncate_tokens("\n".join(reversed(rows)), token_budget)


def assemble_main_context(
    store: Store,
    retrieval: dict[str, object],
    summary_token_budget: int,
    recent_before_timestamp: float | None = None,
) -> dict[str, str]:
    return {
        "recent_external_events": assemble_recent_external_events(
            store,
            recent_before_timestamp,
        ),
        "episodes": _episode_context(
            store,
            retrieval.get("episodes"),
            summary_token_budget,
        ),
        "long_term_memories": str(retrieval.get("long_term_memories") or ""),
        "recent_memories": str(retrieval.get("recent_memories") or ""),
        "recall_memories": _memory_lines(retrieval.get("recall_memories")),
        "query_recall": str(retrieval.get("query_recall") or ""),
        "reflection_memories": (
            REFLECTION_MEMORY_CAUTION
            + "\n"
            + _reflection_memory_lines(retrieval.get("reflection_memories"))
            if retrieval.get("reflection_memories")
            else ""
        ),
        "goal_directory": _goal_directory_lines(retrieval.get("goals")),
        "goal_progress": _goal_progress_lines(retrieval.get("goals")),
    }


def recall_episode_context(
    store: Store,
    query: str,
    max_results: int,
    summary_token_budget: int,
    *,
    skip_empty_webhook: bool = False,
    exclude_turn_ids: set[str] | None = None,
) -> str:
    query = query.strip()
    if not query:
        return ""
    episodes = SECTION_BUDGET_ALLOCATOR.select(
        [("query", store.search_episodes(query, max_results))],
        lambda row: row["id"],
        lambda row: truncate_tokens(
            _episode_search_text(row),
            max(1, summary_token_budget // max(1, max_results)),
        ),
        lambda row: {
            "episode_id": row["id"],
            "relation": "recalled",
            "is_new": False,
            "matches": row.get("matches", []),
            "matched_keywords": row.get("matched_keywords", []),
            "keyword_match_count": row.get("keyword_match_count", 0),
            "search_score": row.get("search_score", 0),
        },
        _merge_matches,
        max_results,
        summary_token_budget,
    )
    recent_ids = exclude_turn_ids or set()
    for episode in episodes:
        episode["is_recent"] = any(
            isinstance(match, dict)
            and str(match.get("turn_id") or "") in recent_ids
            for match in episode.get("matches") or []
        )
    episodes = rank_recall_items(episodes)
    return _episode_context(
        store,
        episodes,
        summary_token_budget,
        summary_token_budget,
        skip_empty_webhook=skip_empty_webhook,
    )
