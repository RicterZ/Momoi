from __future__ import annotations

import copy
import json
import logging
import re
import time
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
RECENT_EPISODE_LIMIT = 6
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
    """Apply Planner fan-out while keeping sparse and dense inputs separate."""
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
    recent_episodes = (
        store.list_recent_episodes(
            time.time() - config.recent_episode_hours * 3600,
            RECENT_EPISODE_LIMIT,
        )
        if config.recent_episode_hours > 0 and config.summary_tokens > 0
        else []
    )
    recent_episode_ids = {str(episode["id"]) for episode in recent_episodes}
    episodes = [
        {
            "episode_id": str(episode["id"]),
            "relation": "recent",
            "is_new": False,
            "matches": [],
            "unit_ids": [],
            "last_activity_at": float(episode.get("last_activity_at") or 0),
            "matched_keywords": [],
            "keyword_match_count": 0,
            "search_score": 0.0,
            "is_recent": True,
        }
        for episode in recent_episodes
    ]
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
            "content": truncate_tokens(str(row.get("content") or ""), 160),
            "unit_ids": list(row.get("unit_ids") or []),
        }
        for row in ranked_memories
        if row.get("source") == "confirmed"
    ]
    reflection_memories = [
        {
            "kind": truncate_tokens(str(row.get("kind") or ""), 24),
            "key": truncate_tokens(str(row.get("key") or ""), 64),
            "content": truncate_tokens(str(row.get("content") or ""), 160),
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

    # Query-specific episodes supplement the time-window directory, without
    # duplicating an episode already selected by recency.
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
        "recent_memories": store.recent_memory_context(
            max(100, config.memory_tokens // 8)
        ),
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


def _goal_lines(items: object) -> str:
    if not isinstance(items, list):
        return ""
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        fields = [
            f"id={item['id']}",
            f"status={item.get('status') or 'unknown'}",
            f"title={truncate_tokens(str(item.get('title') or ''), 80)}",
        ]
        for key, label, limit in (
            ("next_action", "next", 100),
            ("waiting_for", "waiting", 80),
            ("latest_result", "last", 100),
            ("blocked_reason", "blocked", 80),
        ):
            value = item.get(key)
            if value not in (None, "", [], {}):
                fields.append(f"{label}={truncate_tokens(str(value), limit)}")
        lines.append("- " + " ".join(fields))
    return "\n".join(lines)


def _message_role(message: dict[str, object]) -> str:
    role = str(message.get("role") or "").upper()
    state = str(message.get("delivery_state") or "")
    if role == "EVENT":
        return "EVENT channel=webhook"
    if state == "uncertain":
        return f"{role} delivery=uncertain"
    if state == "internal":
        return f"{role} visibility=internal"
    return role


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


def _episode_context(
    store: Store,
    episodes: object,
    summary_token_budget: int,
    _raw_token_budget: int = 0,
    _exclude_message_ids: set[int] | None = None,
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
        if summary:
            lines.append(f"summary: {truncate_tokens(summary, per_summary)}")
        if episode["topics"]:
            lines.append(f"topics: {json.dumps(episode['topics'], ensure_ascii=False)}")
        if episode["open_loops"]:
            lines.append(
                f"open_loops: {json.dumps(episode['open_loops'], ensure_ascii=False)}"
            )
        sections.append("\n".join(lines))
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


def assemble_recent_conversation(
    store: Store,
    turn_limit: int,
    token_budget: int,
    before_timestamp: float | None = None,
) -> tuple[str, set[int]]:
    recent_messages = store.recent_conversation_messages(
        turn_limit, token_budget, before_timestamp
    )
    recent = "\n".join(
        f"[{_message_role(message)} "
        f"timestamp={message.get('timestamp') or context_timestamp(message['created_at'])} "
        f"turn={message['turn_id']}] "
        f"{str(message['content'] or '')}"
        for message in recent_messages
    )
    return recent, {int(message["id"]) for message in recent_messages}


def assemble_compact_recent_conversation(
    store: Store,
    turn_limit: int = 2,
    token_budget: int = 1600,
    before_timestamp: float | None = None,
) -> str:
    """Render the latest shared Turns as compact continuity evidence.

    This groups messages by Turn and keeps one timestamp plus role-labelled
    lines. It is intentionally
    bounded because it is shared by Planner and Heartbeat inputs.
    """
    if turn_limit <= 0:
        return "(none)"
    messages = store.recent_conversation_messages(
        turn_limit, max(1, token_budget), before_timestamp
    )
    if not messages:
        return "(none)"
    blocks: list[str] = []
    current_id = ""
    lines: list[str] = []
    for message in messages:
        turn_id = str(message.get("turn_id") or "")
        if turn_id != current_id:
            if lines:
                blocks.append("\n".join(lines))
            current_id = turn_id
            timestamp = message.get("timestamp") or context_timestamp(message["created_at"])
            lines = [f"Turn {timestamp}"]
        role = str(message.get("role") or "message").lower()
        if role == "event":
            role = "event"
        elif role not in {"user", "assistant"}:
            role = "message"
        content = str(message.get("content") or "")
        lines.append(f"  {role}: {truncate_tokens(' '.join(content.split()), 220)}")
    if lines:
        blocks.append("\n".join(lines))
    rendered = "\n\n".join(blocks)
    return truncate_tokens(rendered, max(1, token_budget)) if rendered else "(none)"


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
                if str(item.get("name") or "") == "send_message":
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


def _compact_turn_record(
    record: dict[str, object], token_budget: int
) -> dict[str, object]:
    compact = copy.deepcopy(record)
    timeline = compact.get("timeline")
    if not isinstance(timeline, list):
        return compact
    per_item = max(32, token_budget // max(1, len(timeline)))
    for item in timeline:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("text"), str):
            item["text"] = truncate_tokens(str(item["text"]), per_item)
        if item.get("type") == "tool_result" and "result" in item:
            rendered = json.dumps(
                item["result"],
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            if estimate_tokens(rendered) > per_item:
                item["result"] = {
                    "ok": item.get("ok"),
                    "error": item.get("error"),
                    "summary": truncate_tokens(rendered, per_item),
                    "truncated": True,
                }
        if item.get("type") == "tool_call" and "arguments" in item:
            rendered = json.dumps(
                item["arguments"],
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            if estimate_tokens(rendered) > per_item:
                item["arguments"] = {
                    "summary": truncate_tokens(rendered, per_item),
                    "truncated": True,
                }
    return compact


_OWNER_HISTORY_RESULT_TOKENS = 96
_OWNER_HISTORY_ARGUMENT_TOKENS = 64


def _short_identifier(value: object, *, prefix: str = "") -> str:
    text = str(value or "")
    if not text:
        return ""
    return f"{prefix}{text[-8:]}"


def _owner_history_argument(name: str, arguments: object) -> str:
    if not isinstance(arguments, dict):
        return ""
    keep: tuple[str, ...]
    if name in {"memory_search", "episode_search", "thinking_search"}:
        keep = ("query", "limit")
    elif name in {"episode_read", "thinking_read"}:
        keep = ("episode_id", "message_id", "content_offset", "before_ordinal")
    elif name in {"goal_create", "goal_update", "goal_finish", "goal_cancel"}:
        keep = ("goal_id", "title", "status", "next_action")
    elif name in {"send_message", "owner_notify"}:
        messages = arguments.get("messages")
        if isinstance(messages, list):
            return f"messages={len(messages)}"
        return ""
    elif name in {
        "curl",
        "read_file",
        "list_dir",
        "write_file",
        "apply_patch",
        "makedirs",
        "move_file",
        "delete_file",
    }:
        keep = ("url", "path", "source", "destination", "query")
    else:
        keep = ("query", "keyword", "limit", "path", "url", "id")
    selected = [
        (key, arguments[key])
        for key in keep
        if arguments.get(key) not in (None, "", [], {})
    ]
    if not selected:
        return ""
    parts: list[str] = []
    for key, value in selected:
        if isinstance(value, list):
            text = " | ".join(str(item) for item in value)
        elif isinstance(value, dict):
            text = " ".join(
                f"{nested_key}:{nested_value}"
                for nested_key, nested_value in value.items()
                if nested_value not in (None, "", [], {})
            )
        else:
            text = str(value)
        parts.append(f"{key}={' '.join(text.split())}")
    rendered = " ".join(parts)
    return truncate_tokens(rendered, _OWNER_HISTORY_ARGUMENT_TOKENS)


def _owner_history_text(value: str, limit: int) -> str:
    """Collapse structured tool text into readable facts instead of JSON blobs."""
    text = value.strip()
    if text[:1] in {"{", "["}:
        structured_text = (
            text.replace("\\r\\n", "\n")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
        )
        try:
            parsed = json.loads(structured_text)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            parts: list[str] = []
            for key, item in parsed.items():
                if isinstance(item, list):
                    parts.append(f"{key}={len(item)} items")
                elif isinstance(item, dict):
                    parts.append(f"{key}={len(item)} fields")
                elif item not in (None, "", [], {}):
                    parts.append(f"{key}={item}")
            if parts:
                return truncate_tokens("; ".join(parts), limit)
        elif isinstance(parsed, list):
            return f"{len(parsed)} items"
    return truncate_tokens(" ".join(text.split()), limit)


def _owner_history_result(name: str, value: object, ok: object = True) -> str:
    if not ok:
        if isinstance(value, dict):
            error = value.get("error") or value.get("message") or "failed"
        else:
            error = value or "failed"
        return f"error={truncate_tokens(str(error), 80)}"
    if not isinstance(value, dict):
        return truncate_tokens(str(value or ""), _OWNER_HISTORY_RESULT_TOKENS)
    if value.get("error"):
        return f"error={truncate_tokens(str(value['error']), 80)}"
    parts: list[str] = []
    if value.get("truncated"):
        parts.append("truncated=true")
        original_chars = value.get("original_chars")
        if original_chars:
            parts.append(f"original_chars={original_chars}")
        result_ref = value.get("result_ref")
        if result_ref:
            parts.append(f"result_ref={result_ref}")
    for key in ("state", "status", "count", "next_cursor"):
        item = value.get(key)
        if item not in (None, "", [], {}):
            parts.append(f"{key}={item}")
    nested = value.get("goal") or value.get("memory")
    if isinstance(nested, dict):
        for key in ("id", "title", "key", "kind", "activation", "status", "next_action"):
            item = nested.get(key)
            if item not in (None, "", [], {}):
                parts.append(f"{key}={truncate_tokens(str(item), 64)}")
    nested_result = value.get("result") or value.get("data")
    if isinstance(nested_result, dict):
        for key in ("state", "status", "count", "title", "id"):
            item = nested_result.get(key)
            if item not in (None, "", [], {}) and f"{key}={item}" not in parts:
                parts.append(f"{key}={truncate_tokens(str(item), 64)}")
    elif isinstance(nested_result, str) and nested_result.strip():
        parts.append(f"summary={_owner_history_text(nested_result, 80)}")
    elif isinstance(nested_result, list):
        parts.append(f"items_count={len(nested_result)}")
    results = value.get("results")
    if not isinstance(results, list):
        for candidate_key in ("items", "posts", "entries", "articles", "messages"):
            candidate = value.get(candidate_key)
            if isinstance(candidate, list):
                results = candidate
                parts.append(f"{candidate_key}_count={len(candidate)}")
                break
    if isinstance(results, list) and results:
        labels: list[str] = []
        for item in results[:3]:
            if not isinstance(item, dict):
                continue
            label = item.get("title") or item.get("key") or item.get("id")
            summary = item.get("summary") or item.get("content")
            if label:
                labels.append(str(label))
            elif summary:
                labels.append(truncate_tokens(str(summary), 48))
        if labels:
            parts.append("hits=" + " | ".join(labels))
    for key in ("message", "body", "content"):
        if value.get("truncated") and key in {"body", "content"}:
            continue
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            parts.append(f"{key}={_owner_history_text(item, 80)}")
            break
    return truncate_tokens(" ".join(parts) or "ok", _OWNER_HISTORY_RESULT_TOKENS)


def _owner_history_summary(name: str, value: object, ok: object = True) -> str:
    """Return one semantic sentence for cross-turn tool continuity."""
    if not ok:
        return f"{name} failed"
    if isinstance(value, dict):
        if value.get("truncated"):
            original_chars = value.get("original_chars")
            suffix = f" ({original_chars} chars)" if original_chars else ""
            return f"{name} returned truncated result{suffix}"
        memory = value.get("memory")
        if isinstance(memory, dict):
            key = memory.get("key") or "unknown"
            state = str(value.get("state") or "saved")
            verb = "staged" if state == "staged" else "updated"
            return f"{verb} memory {key}"
        if value.get("forgotten") or str(value.get("state") or "") == "forgotten":
            return f"forgot memory {value.get('key') or value.get('id') or 'item'}"
        for key, label in (("count", "returned"), ("items_count", "returned")):
            if value.get(key) is not None:
                return f"{name} {label} {value[key]} items"
        for key in ("summary", "message", "content", "body", "result"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return _owner_history_text(item, 48)
        nested = value.get("data")
        if isinstance(nested, dict):
            return _owner_history_summary(name, nested, ok)
        if isinstance(nested, list):
            return f"{name} returned {len(nested)} items"
    if isinstance(value, str) and value.strip():
        return _owner_history_text(value, 48)
    return f"{name} completed"


def _owner_history_line(item: dict[str, object], call_names: dict[str, str]) -> str:
    item_type = str(item.get("type") or "")
    if item_type in {"owner_message", "event", "assistant_message"}:
        role = {
            "owner_message": "owner",
            "assistant_message": "momoi",
            "event": "event",
        }[item_type]
        delivery = str(item.get("delivery") or "")
        suffix = f" [{delivery}]" if delivery not in {"", "delivered"} else ""
        text = str(item.get("text") or "")
        if item_type == "owner_message":
            text = _owner_message_text(text)
        return f"{role}{suffix}: {text}"
    if item_type == "tool_call":
        call = _short_identifier(item.get("tool_call_id") or item.get("call"), prefix="c-")
        name = str(item.get("name") or "tool")
        call_names[str(item.get("tool_call_id") or item.get("call") or "")] = name
        args = _owner_history_argument(name, item.get("arguments"))
        return f"call {call} {name}{' ' + args if args else ''}"
    if item_type == "tool_result":
        raw_call = str(item.get("tool_call_id") or item.get("call") or "")
        call = _short_identifier(raw_call, prefix="c-")
        name = call_names.get(raw_call) or str(item.get("name") or "tool")
        value = item.get("result")
        details = _owner_history_result(name, value, item.get("ok", True))
        summary = _owner_history_summary(name, value, item.get("ok", True))
        return f"result {call} {name}: summary={summary}; {details}"
    return ""


def project_recent_turns_for_owner(
    document: dict[str, object],
    token_budget: int | None,
    *,
    start_index: int = 1,
) -> str:
    """Render recent history as a causal, owner-facing text projection.

    Planner history remains structured JSON because it needs machine-readable
    intent and tool references. Owner Turns need evidence and continuity, not a
    replay of the runtime journal, so tool payloads are reduced to one line.
    """
    turns = document.get("turns")
    if not isinstance(turns, list) or (
        token_budget is not None and token_budget <= 0
    ):
        return ""
    blocks: list[str] = []
    for index, raw_turn in enumerate(turns[-6:], start=start_index):
        if not isinstance(raw_turn, dict):
            continue
        at = raw_turn.get("started_at") or raw_turn.get("completed_at") or raw_turn.get("at")
        kind = str(raw_turn.get("kind") or "owner")
        header = f"T-{index}"
        if at:
            header += f" {str(at)[:16]}"
        if kind != "owner":
            header += f" [{kind}]"
        lines = [header]
        call_names: dict[str, str] = {}
        for item in raw_turn.get("timeline") or []:
            if not isinstance(item, dict):
                continue
            line = _owner_history_line(item, call_names)
            if line:
                lines.append(f"  {line}")
        final = raw_turn.get("final")
        if isinstance(final, dict):
            if final.get("failure"):
                lines.append(f"  final: failure={truncate_tokens(str(final['failure']), 96)}")
            mutations = final.get("mutations")
            if isinstance(mutations, dict):
                changed = [key for key, value in mutations.items() if value not in (None, "", [], {})]
                if changed:
                    lines.append("  final: changed=" + ",".join(changed))
            if final.get("external_effect"):
                lines.append("  final: external_effect=true")
            mutations = final.get("mutations")
            if isinstance(mutations, dict):
                for mutation_name in ("memories", "forgotten_memories", "goals"):
                    entries = mutations.get(mutation_name)
                    if not isinstance(entries, list) or not entries:
                        continue
                    labels: list[str] = []
                    for entry in entries[:4]:
                        if not isinstance(entry, dict):
                            continue
                        if mutation_name in {"memories", "forgotten_memories"}:
                            label = f"{entry.get('kind', '')}:{entry.get('key', '')}"
                        else:
                            label = str(entry.get("id") or entry.get("goal_id") or "")
                        if label.strip(":"):
                            labels.append(truncate_tokens(label, 64))
                    if labels:
                        lines.append(f"  final: {mutation_name}=" + ",".join(labels))
        blocks.append("\n".join(lines))
    rendered = "\n\n".join(blocks)
    return (
        rendered
        if token_budget is None
        else truncate_tokens(rendered, token_budget)
    )


def assemble_recent_turns(
    store: Store,
    turn_limit: int,
    token_budget: int | None,
    before_timestamp: float | None = None,
) -> tuple[dict[str, object], str]:
    if turn_limit <= 0 or (
        token_budget is not None and token_budget <= 0
    ):
        empty: dict[str, object] = {"version": 1, "turns": []}
        return empty, json.dumps(empty, separators=(",", ":"))
    records = store.recent_turn_records(turn_limit, before_timestamp)
    selected: list[dict[str, object]] = []
    used = 0
    for record in reversed(records):
        rendered = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        size = estimate_tokens(rendered)
        candidate = record
        if token_budget is not None and not selected and size > token_budget:
            candidate = _compact_turn_record(record, token_budget)
            rendered = json.dumps(
                candidate,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            size = estimate_tokens(rendered)
        if token_budget is not None and selected and used + size > token_budget:
            break
        selected.append(candidate)
        used += size
    selected.reverse()
    document: dict[str, object] = {"version": 1, "turns": selected}
    rendered = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return document, rendered


def _owner_message_text(value: object) -> str:
    text = str(value or "")
    return re.sub(
        r"(?m)^(\d{4}-\d{2}-\d{2}T\S+)\s+\[[^\]\n]+\]\s*",
        r"\1 ",
        text,
    )


def _planner_message_text(value: object) -> str:
    text = str(value or "")
    return re.sub(
        r"(?m)^\d{4}-\d{2}-\d{2}T\S+(?:\s+\[[^\]\n]+\])?\s*",
        "",
        text,
    )


def _planner_interpretation(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    raw_intents = value.get("intents")
    intents: list[dict[str, object]] = []
    unit_indexes: dict[str, int] = {}
    for raw in raw_intents if isinstance(raw_intents, list) else []:
        if not isinstance(raw, dict):
            continue
        unit_id = str(raw.get("id") or "")
        intent = {
            key: copy.deepcopy(raw[key])
            for key in ("intent", "speech_act", "references")
            if raw.get(key) not in (None, "", [], {})
        }
        if intent:
            if unit_id:
                unit_indexes[unit_id] = len(intents)
            intents.append(intent)

    raw_actions = value.get("episode_actions")
    actions: list[dict[str, object]] = []
    for raw in raw_actions if isinstance(raw_actions, list) else []:
        if not isinstance(raw, dict):
            continue
        action = {
            key: copy.deepcopy(raw[key])
            for key in ("action", "episode_id", "episode_ref", "title")
            if raw.get(key) not in (None, "", [], {})
        }
        indexes = [
            unit_indexes[str(unit_id)]
            for unit_id in raw.get("unit_ids") or []
            if str(unit_id) in unit_indexes
        ]
        if indexes:
            action["intent_indexes"] = indexes
        if action:
            actions.append(action)

    projected: dict[str, object] = {}
    if intents:
        projected["intents"] = intents
    if actions:
        projected["episode_actions"] = actions
    uncertainty = value.get("uncertainty")
    if uncertainty:
        projected["uncertainty"] = copy.deepcopy(uncertainty)
    return projected


def _planner_final(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    projected: dict[str, object] = {}
    if value.get("external_effect"):
        projected["external_effect"] = True
    if value.get("failure"):
        projected["failure"] = copy.deepcopy(value["failure"])
    reply_wait = value.get("reply_wait")
    if isinstance(reply_wait, dict) and reply_wait.get("wait"):
        projected["reply_wait"] = copy.deepcopy(reply_wait)
    if value.get("mood_change") is not None:
        projected["mood_change"] = copy.deepcopy(value["mood_change"])
    if value.get("plan_adjustment"):
        projected["plan_adjustment"] = copy.deepcopy(value["plan_adjustment"])
    mutations = value.get("mutations")
    if isinstance(mutations, dict):
        nonempty = {
            key: copy.deepcopy(item)
            for key, item in mutations.items()
            if item not in (None, "", [], {})
        }
        if nonempty:
            projected["mutations"] = nonempty
    return projected


def _planner_tail(text: str, token_budget: int) -> str:
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high) // 2
        if estimate_tokens(text[middle:]) <= token_budget:
            high = middle
        else:
            low = middle + 1
    return text[low:]


def _planner_clip_text(text: str, token_budget: int) -> object:
    original_tokens = estimate_tokens(text)
    if original_tokens <= token_budget:
        return text
    head_budget = max(1, token_budget * 2 // 3)
    tail_budget = max(1, token_budget - head_budget)
    return {
        "truncated": True,
        "original_tokens": original_tokens,
        "shown_tokens": token_budget,
        "head": truncate_tokens(text, head_budget),
        "tail": _planner_tail(text, tail_budget),
    }


def _planner_clip_list(
    values: list[object],
    token_budget: int,
    *,
    item_limit: int | None = None,
) -> object:
    original_tokens = estimate_tokens(
        json.dumps(values, ensure_ascii=False, separators=(",", ":"), default=str)
    )
    limit = len(values) if item_limit is None else min(len(values), item_limit)
    if original_tokens <= token_budget and len(values) <= limit:
        return copy.deepcopy(values)
    head_count = max(1, limit * 4 // 5)
    tail_count = max(0, limit - head_count)
    selected = [
        *copy.deepcopy(values[:head_count]),
        *(
            copy.deepcopy(values[-tail_count:])
            if tail_count
            else []
        ),
    ]
    while (
        len(selected) > 1
        and estimate_tokens(
            json.dumps(
                selected,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        )
        > token_budget
    ):
        selected.pop(-2 if tail_count and len(selected) > tail_count else -1)
    return {
        "truncated": True,
        "original_items": len(values),
        "original_tokens": original_tokens,
        "items": selected,
    }


def _planner_state_result(value: dict[str, object]) -> dict[str, object]:
    projected = copy.deepcopy(value)
    goal = projected.get("goal")
    if isinstance(goal, dict):
        projected["goal"] = {
            key: copy.deepcopy(goal[key])
            for key in (
                "id",
                "title",
                "status",
                "next_action",
                "waiting_for",
                "blocked_reason",
                "latest_result",
                "schedule",
                "next_review_at",
            )
            if goal.get(key) not in (None, "", [], {})
        }
    memory = projected.get("memory")
    if isinstance(memory, dict):
        projected["memory"] = {
            key: copy.deepcopy(memory[key])
            for key in ("kind", "key", "activation", "ttl_hours")
            if memory.get(key) not in (None, "", [], {})
        }
    return projected


def _planner_tool_result(
    name: str,
    value: object,
    *,
    compact: bool,
) -> object:
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    result = _planner_state_result(value)
    result.pop("provenance", None)
    if result.get("ok") is True:
        result.pop("ok", None)
    if result.get("error") in (None, ""):
        result.pop("error", None)
    if result.get("truncated") is False:
        result.pop("truncated", None)
    if not compact:
        return result

    content = result.get("content")
    if isinstance(content, str):
        limit = 512 if name == "read_file" else 384 if name == "curl" else 512
        result["content"] = _planner_clip_text(content, limit)
    entries = result.get("entries")
    if isinstance(entries, list):
        result["entries"] = _planner_clip_list(entries, 384, item_limit=25)
    results = result.get("results")
    if isinstance(results, list):
        result["results"] = _planner_clip_list(results, 512, item_limit=10)
    mcp_result = result.get("result")
    if name.startswith("mcp__") and estimate_tokens(
        json.dumps(
            mcp_result,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    ) > 512:
        if isinstance(mcp_result, str):
            result["result"] = _planner_clip_text(mcp_result, 512)
        elif isinstance(mcp_result, list):
            result["result"] = _planner_clip_list(mcp_result, 512)
        else:
            result["result"] = _planner_clip_text(
                json.dumps(
                    mcp_result,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ),
                512,
            )
    rendered = json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    if estimate_tokens(rendered) > 768:
        return _planner_clip_text(rendered, 512)
    return result


def project_recent_turns_for_planner(
    document: dict[str, object],
    *,
    compact_tool_results: bool = False,
) -> dict[str, object]:
    """Keep planner-relevant history with a deterministic tool-result policy."""
    projected_turns: list[dict[str, object]] = []
    turns = document.get("turns")
    for raw_turn in turns if isinstance(turns, list) else []:
        if not isinstance(raw_turn, dict):
            continue
        turn: dict[str, object] = {
            "turn_id": str(raw_turn.get("turn_id") or ""),
        }
        if raw_turn.get("kind") not in (None, "", "owner"):
            turn["kind"] = copy.deepcopy(raw_turn["kind"])
        if raw_turn.get("state") not in (None, "", "completed"):
            turn["state"] = copy.deepcopy(raw_turn["state"])
        raw_timeline = raw_turn.get("timeline")
        at = raw_turn.get("started_at") or raw_turn.get("completed_at")
        if isinstance(raw_timeline, list):
            at = next(
                (
                    item.get("timestamp")
                    for item in raw_timeline
                    if isinstance(item, dict) and item.get("timestamp")
                ),
                at,
            )
        if at:
            turn["at"] = copy.deepcopy(at)
        interpretation = _planner_interpretation(raw_turn.get("interpretation"))
        if interpretation:
            turn["interpretation"] = interpretation
        final = _planner_final(raw_turn.get("final"))
        if final:
            turn["final"] = final

        call_ids: dict[str, str] = {}
        call_names: dict[str, str] = {}

        def call_ref(value: object) -> str:
            raw = str(value or "")
            if not raw:
                return ""
            if raw not in call_ids:
                call_ids[raw] = f"t{len(call_ids) + 1}"
            return call_ids[raw]

        timeline: list[dict[str, object]] = []
        for raw_item in raw_timeline if isinstance(raw_timeline, list) else []:
            if not isinstance(raw_item, dict):
                continue
            item_type = str(raw_item.get("type") or "")
            if item_type == "tool_call":
                raw_call_id = str(raw_item.get("tool_call_id") or "")
                name = str(raw_item.get("name") or "")
                if raw_call_id:
                    call_names[raw_call_id] = name
                timeline.append(
                    {
                        "type": item_type,
                        "call": call_ref(raw_call_id),
                        "name": name,
                        "arguments": copy.deepcopy(raw_item.get("arguments")),
                    }
                )
                continue
            if item_type == "tool_result":
                raw_call_id = str(raw_item.get("tool_call_id") or "")
                name = call_names.get(raw_call_id) or str(
                    raw_item.get("name") or ""
                )
                result_item: dict[str, object] = {
                    "type": item_type,
                    "call": call_ref(raw_call_id),
                }
                if not raw_call_id and name:
                    result_item["name"] = name
                if raw_item.get("ok") is False:
                    result_item["error"] = copy.deepcopy(
                        raw_item.get("error") or "tool_failed"
                    )
                projected_result = _planner_tool_result(
                    name,
                    raw_item.get("result"),
                    compact=compact_tool_results,
                )
                if projected_result not in (None, "", [], {}):
                    result_item["result"] = projected_result
                timeline.append(result_item)
                continue
            if item_type in {"owner_message", "assistant_message", "event"}:
                message: dict[str, object] = {
                    "type": item_type,
                    "text": _planner_message_text(raw_item.get("text")),
                }
                delivery = str(raw_item.get("delivery") or "delivered")
                if delivery != "delivered":
                    message["delivery"] = delivery
                timeline.append(message)
                continue
            timeline.append(copy.deepcopy(raw_item))
        turn["timeline"] = timeline
        projected_turns.append(turn)
    return {
        "version": document.get("version", 1),
        "turns": projected_turns,
    }


def _recent_turn_cache_block_records(
    store: Store,
    base_turns: int,
    append_turns: int,
    before_timestamp: float | None = None,
) -> tuple[list[dict[str, object]], int]:
    total = store.recent_turn_record_count(before_timestamp)
    phase = total % append_turns
    turn_limit = base_turns if phase == 0 else base_turns + phase
    records = store.recent_turn_records(turn_limit, before_timestamp)
    base_count = len(records) if phase == 0 else max(0, len(records) - phase)
    return records, base_count


def assemble_planner_recent_turns(
    store: Store,
    base_turns: int,
    append_turns: int,
    active_turns: int,
    token_budget: int,
    before_timestamp: float | None = None,
) -> tuple[dict[str, object], list[str], int]:
    base_turns = max(1, int(base_turns))
    append_turns = max(1, int(append_turns))
    active_turns = max(1, int(active_turns))
    token_budget = max(1, int(token_budget))

    raw_turns, base_count = _recent_turn_cache_block_records(
        store,
        base_turns,
        append_turns,
        before_timestamp,
    )
    phase = len(raw_turns) - base_count
    base_turn_ids = {
        str(turn.get("turn_id") or "")
        for turn in raw_turns[:base_count]
        if str(turn.get("turn_id") or "")
    }
    projected = project_recent_turns_for_planner(
        {"version": 1, "turns": raw_turns},
        compact_tool_results=True,
    )
    projected_turns = projected["turns"]
    if not isinstance(projected_turns, list):
        projected_turns = []

    def size(turn: dict[str, object]) -> int:
        return estimate_tokens(
            json.dumps(
                turn,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        )

    envelope = estimate_tokens('{"version":1,"turns":[]}')
    if (
        phase > 0
        and len(projected_turns) > phase
        and envelope
        + sum(size(turn) for turn in projected_turns if isinstance(turn, dict))
        > token_budget
    ):
        raw_turns = raw_turns[-phase:]
        projected_turns = projected_turns[-phase:]

    selected: list[dict[str, object]] = []
    used = envelope
    for raw_turn, turn in reversed(list(zip(raw_turns, projected_turns))):
        if not isinstance(turn, dict):
            continue
        turn_size = size(turn)
        if selected and used + turn_size > token_budget:
            break
        if not selected and used + turn_size > token_budget:
            compact = _compact_turn_record(
                raw_turn,
                max(1, token_budget - envelope),
            )
            compact_document = project_recent_turns_for_planner(
                {"version": 1, "turns": [compact]},
                compact_tool_results=True,
            )
            compact_turns = compact_document.get("turns")
            if isinstance(compact_turns, list) and compact_turns:
                turn = compact_turns[0]
                turn_size = size(turn)
        selected.append(turn)
        used += turn_size
    selected.reverse()
    document: dict[str, object] = {"version": 1, "turns": selected}
    active_ids = [
        str(turn.get("turn_id") or "")
        for turn in selected[-active_turns:]
        if str(turn.get("turn_id") or "")
    ]
    selected_base_count = sum(
        1
        for turn in selected
        if str(turn.get("turn_id") or "") in base_turn_ids
    )
    return document, active_ids, selected_base_count


def render_planner_recent_turns(
    document: dict[str, object],
    *,
    start_index: int = 1,
) -> str:
    """Render planner history as readable evidence instead of nested JSON."""
    turns = document.get("turns")
    if not isinstance(turns, list):
        return ""
    blocks: list[str] = []
    for index, turn in enumerate(turns, start=start_index):
        if not isinstance(turn, dict):
            continue
        header = f"T-{index}"
        if turn.get("at"):
            header += f" {str(turn['at'])[:16]}"
        if turn.get("kind"):
            header += f" [{turn['kind']}]"
        lines = [header]
        interpretation = turn.get("interpretation")
        if isinstance(interpretation, dict):
            intents = interpretation.get("intents")
            if isinstance(intents, list):
                for intent in intents[:3]:
                    if isinstance(intent, dict) and intent.get("intent"):
                        lines.append(
                            "  intent: " + truncate_tokens(str(intent["intent"]), 120)
                        )
        for item in turn.get("timeline") or []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type in {"owner_message", "assistant_message", "event"}:
                role = {
                    "owner_message": "owner",
                    "assistant_message": "momoi",
                    "event": "event",
                }[item_type]
                delivery = str(item.get("delivery") or "")
                if delivery not in {"", "delivered"}:
                    role += f" [{delivery}]"
                lines.append(f"  {role}: {str(item.get('text') or '')}")
            elif item_type == "tool_call":
                args = _owner_history_argument(
                    str(item.get("name") or "tool"), item.get("arguments")
                )
                lines.append(
                    f"  call {item.get('call') or 'c'} {item.get('name') or 'tool'}"
                    + (f" {args}" if args else "")
                )
            elif item_type == "tool_result":
                name = str(item.get("name") or "tool")
                value = item.get("result")
                lines.append(
                    f"  result {item.get('call') or 'c'} {name}: "
                    f"summary={_owner_history_summary(name, value, not bool(item.get('error')))}; "
                    f"{_owner_history_result(name, value, not bool(item.get('error')))}"
                )
        final = turn.get("final")
        if isinstance(final, dict) and final.get("failure"):
            lines.append(f"  final: failure={truncate_tokens(str(final['failure']), 96)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_planner_recent_turn_focus(
    document: dict[str, object],
    active_turn_ids: list[str],
) -> str:
    active = {str(value) for value in active_turn_ids}
    turns = document.get("turns")
    if not isinstance(turns, list):
        return ""
    labels = [
        f"T-{index}"
        for index, turn in enumerate(turns, start=1)
        if isinstance(turn, dict)
        and str(turn.get("turn_id") or "") in active
    ]
    return ", ".join(labels)


def assemble_main_context(
    store: Store,
    retrieval: dict[str, object],
    summary_token_budget: int,
    raw_token_budget: int,
    recent_turns: int = 0,
    recent_before_timestamp: float | None = None,
) -> dict[str, str]:
    if recent_turns > 0:
        raw_recent_turns, recent_turn_base_count = (
            _recent_turn_cache_block_records(
                store,
                recent_turns,
                recent_turns,
                recent_before_timestamp,
            )
        )
    else:
        raw_recent_turns, recent_turn_base_count = [], 0
    recent_turn_base = project_recent_turns_for_owner(
        {
            "version": 1,
            "turns": raw_recent_turns[:recent_turn_base_count],
        },
        None,
    )
    recent_turn_append = project_recent_turns_for_owner(
        {
            "version": 1,
            "turns": raw_recent_turns[recent_turn_base_count:],
        },
        None,
        start_index=recent_turn_base_count + 1,
    )
    compact_recent_turns = "\n\n".join(
        value for value in (recent_turn_base, recent_turn_append) if value
    )
    compact_recent_conversation = assemble_compact_recent_conversation(
        store,
        min(4, recent_turns),
        min(1600, max(400, raw_token_budget // 3)),
        recent_before_timestamp,
    )
    return {
        "recent_turn_base": recent_turn_base,
        "recent_turn_append": recent_turn_append,
        "recent_external_events": assemble_recent_external_events(
            store,
            recent_before_timestamp,
        ),
        "recent_turns": compact_recent_turns,
        "recent_conversation": compact_recent_conversation,
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
        "goals": _goal_lines(retrieval.get("goals")),
        "goal_directory": _goal_directory_lines(retrieval.get("goals")),
        "goal_progress": _goal_progress_lines(retrieval.get("goals")),
    }


def recall_episode_context(
    store: Store,
    query: str,
    max_results: int,
    summary_token_budget: int,
    raw_token_budget: int,
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
        raw_token_budget,
        skip_empty_webhook=skip_empty_webhook,
    )
