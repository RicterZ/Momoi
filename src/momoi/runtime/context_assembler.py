import copy
import json
import logging
import re
import time

from ..config import AppConfig
from ..context_time import context_timestamp
from ..logging_context import log_event, safe_preview
from ..storage import Store, estimate_tokens, truncate_tokens
from .budget import SECTION_BUDGET_ALLOCATOR


logger = logging.getLogger(__name__)
RECENT_EPISODE_LIMIT = 6
RECALLED_EPISODE_LIMIT = 6
RECALLED_TURN_CONTEXT_TOKENS = 6000
RECALLED_TURN_LIMIT = 6
PLAN_RECALL_QUERY_LIMIT = 6


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
) -> dict[str, object]:
    recent_turn_ids = {
        str(item.get("turn_id") or "")
        for item in store.recent_turn_records(config.recent_turns)
        if item.get("turn_id")
    }
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
    reminders = [
        {
            name: row.get(name)
            for name in ("id", "text", "fire_at", "fire_timestamp", "schedule")
        }
        for row in store.list_reminders(
            config.policies.context.max_visible_reminders
        )
    ]
    # Every search-mode unit carries one or more recall expressions. Execute
    # them fairly across units: take each unit's first expression before any
    # unit's second, deduplicate shared expressions, then enforce the global
    # fan-out.
    recall_queries: list[tuple[str, list[str]]] = []
    recall_units = list(plan.get("intent_units") or [])
    activity = plan.get("activity")
    if isinstance(activity, dict) and activity.get("recall_queries"):
        recall_units.append(
            {
                "id": "heartbeat_activity",
                "recall_queries": activity["recall_queries"],
            }
        )
    unit_queries: list[tuple[str, list[str]]] = []
    for unit in recall_units:
        if not isinstance(unit, dict):
            continue
        unit_id = str(unit.get("id") or "")
        queries: list[str] = []
        for raw_query in unit.get("recall_queries") or []:
            query = " ".join(str(raw_query).split())[:120]
            if not query or query in queries:
                continue
            queries.append(query)
        if queries:
            unit_queries.append((unit_id, queries))

    emitted_queries = {
        query for _unit_id, queries in unit_queries for query in queries
    }
    selected_by_query: dict[str, list[str]] = {}
    skipped_unit_ids: set[str] = set()
    max_query_rank = max((len(queries) for _, queries in unit_queries), default=0)
    for query_rank in range(max_query_rank):
        for unit_id, queries in unit_queries:
            if query_rank >= len(queries):
                continue
            query = queries[query_rank]
            selected_units = selected_by_query.get(query)
            if selected_units is not None:
                if unit_id and unit_id not in selected_units:
                    selected_units.append(unit_id)
                continue
            if len(recall_queries) >= PLAN_RECALL_QUERY_LIMIT:
                if unit_id:
                    skipped_unit_ids.add(unit_id)
                continue
            selected_units = [unit_id] if unit_id else []
            selected_by_query[query] = selected_units
            recall_queries.append((query, selected_units))

    recall_memories: list[dict[str, object]] = []
    reflection_memories: list[dict[str, object]] = []
    recalled_episode_rows: dict[str, dict[str, object]] = {}
    recall_hits: list[str] = []
    recall_misses: list[str] = []
    memory_hit_queries: list[str] = []
    reflection_hit_queries: list[str] = []
    topic_hit_queries: list[str] = []
    turn_hit_queries: list[str] = []
    for query, unit_ids in recall_queries:
        memory_limit = min(3, max(1, config.memory_results))
        memory_rows = store.search_memories(
            query, memory_limit, activation="recall"
        )
        reflection_rows = store.search_reflection_memories(
            query,
            min(3, max(1, config.memory_results)),
            include_core=True,
            core_match_only=True,
        )
        episode_rows = store.search_episodes(query, min(3, max(1, config.summary_results)))
        query_hit = False
        memory_hit = bool(memory_rows)
        reflection_hit = bool(reflection_rows)
        topic_hit = bool(episode_rows)
        turn_hit = any(bool(row.get("matches")) for row in episode_rows)
        for row in memory_rows:
            key = (str(row.get("kind") or ""), str(row.get("key") or ""))
            existing = next(
                (item for item in recall_memories if (str(item.get("kind") or ""), str(item.get("key") or "")) == key),
                None,
            )
            if existing is not None:
                query_hit = True
                units = set(existing.get("unit_ids") or [])
                units.update(unit_ids)
                existing["unit_ids"] = sorted(units)
                continue
            item = {
                "kind": truncate_tokens(str(row.get("kind") or ""), 24),
                "key": truncate_tokens(str(row.get("key") or ""), 64),
                "content": truncate_tokens(str(row.get("content") or ""), 160),
                "unit_ids": sorted(unit_ids),
            }
            recall_memories.append(item)
            query_hit = True
        for row in reflection_rows:
            key = (str(row.get("kind") or ""), str(row.get("key") or ""))
            existing = next(
                (item for item in reflection_memories if (str(item.get("kind") or ""), str(item.get("key") or "")) == key),
                None,
            )
            if existing is not None:
                query_hit = True
                units = set(existing.get("unit_ids") or [])
                units.update(unit_ids)
                existing["unit_ids"] = sorted(units)
                continue
            reflection_memories.append(
                {
                    "kind": truncate_tokens(str(row.get("kind") or ""), 24),
                    "key": truncate_tokens(str(row.get("key") or ""), 64),
                    "content": truncate_tokens(str(row.get("content") or ""), 160),
                    "unit_ids": sorted(unit_ids),
                }
            )
            query_hit = True
        for row in episode_rows:
            episode_id = str(row.get("id") or "")
            if not episode_id:
                continue
            selected = recalled_episode_rows.setdefault(
                episode_id,
                {
                    "episode_id": episode_id,
                    "relation": "recalled",
                    "is_new": False,
                    "matches": [],
                    "unit_ids": [],
                    "last_activity_at": float(row.get("last_activity_at") or 0),
                    "matched_keywords": [],
                    "keyword_match_count": 0,
                    "search_score": float(row.get("search_score") or 0),
                    "is_recent": episode_id in recent_episode_ids,
                },
            )
            selected["unit_ids"] = sorted(
                {
                    *(str(item) for item in selected.get("unit_ids") or []),
                    *unit_ids,
                }
            )
            for match in row.get("matches") or []:
                if match not in selected["matches"]:
                    selected["matches"].append(match)
            selected["matched_keywords"] = sorted(
                {
                    *(
                        str(item)
                        for item in selected.get("matched_keywords") or []
                    ),
                    *(
                        str(item)
                        for item in row.get("matched_keywords") or []
                    ),
                }
            )
            selected["keyword_match_count"] = len(
                selected["matched_keywords"]
            )
            selected["search_score"] = max(
                float(selected.get("search_score") or 0),
                float(row.get("search_score") or 0),
            )
            selected["is_recent"] = bool(
                selected.get("is_recent")
                or any(
                    isinstance(match, dict)
                    and str(match.get("turn_id") or "") in recent_turn_ids
                    for match in row.get("matches") or []
                )
            )
            query_hit = True
        if memory_hit:
            memory_hit_queries.append(query)
        if reflection_hit:
            reflection_hit_queries.append(query)
        if topic_hit:
            topic_hit_queries.append(query)
        if turn_hit:
            turn_hit_queries.append(query)
        (recall_hits if query_hit else recall_misses).append(query)

    # Query-specific episodes supplement the time-window directory, without
    # duplicating an episode already selected by recency.
    ranked_recalled_episodes = _rank_recall_items(
        list(recalled_episode_rows.values())
    )[:RECALLED_EPISODE_LIMIT]
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
        existing["is_recent"] = True
    episodes = _rank_recall_items(episodes)
    recalled_turns = _collect_recalled_turns(
        ranked_recalled_episodes
    )
    for item in recalled_turns:
        item["is_recent"] = str(item["turn_id"]) in recent_turn_ids
        item["keyword_match_count"] = len(
            item.get("matched_keywords") or []
        )
    recalled_turns = _rank_recall_items(recalled_turns)
    recall_index: list[str] = []
    if recall_queries:
        recall_index.append("queries=" + " | ".join(query for query, _ in recall_queries))
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
        if topic_hit_queries:
            recall_index.append(
                "topic_hits=" + " | ".join(topic_hit_queries)
            )
        if turn_hit_queries:
            recall_index.append(
                "turn_hits=" + " | ".join(turn_hit_queries)
            )
    retrieval = {
        "version": 3,
        "episodes": episodes,
        "long_term_memories": store.always_memory_context(),
        "recent_memories": store.recent_memory_context(
            max(100, config.memory_tokens // 8)
        ),
        "recall_memories": recall_memories[:8],
        "reflection_memories": reflection_memories[:8],
        "recalled_turns": recalled_turns,
        "goals": goals,
        "reminders": reminders,
        "uncertainty": plan.get("uncertainty", []),
        "query_recall": "\n".join(recall_index),
    }
    log_event(
        logger,
        logging.INFO,
        "context_recall",
        stage="context_recall",
        goals=[{"id": item["id"]} for item in goals],
        reminders=[{"id": item["id"]} for item in reminders],
        counts={
            "episodes": len(episodes),
            "goals": len(goals),
            "reminders": len(reminders),
            "recall_queries": len(recall_queries),
            "recall_queries_emitted": len(emitted_queries),
            "recall_query_units_skipped": len(skipped_unit_ids),
            "recall_memory_hits": len(recall_memories),
            "recall_reflection_hits": len(reflection_memories),
            "recall_episode_hits": len(ranked_recalled_episodes),
            "recall_turn_hits": len(recalled_turns),
        },
    )
    log_event(
        logger,
        logging.DEBUG,
        "context_recall_detail",
        stage="context_recall",
        selected=safe_preview(retrieval, 5000),
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


def _collect_recalled_turns(
    episodes: list[dict[str, object]],
) -> list[dict[str, object]]:
    turns: dict[str, dict[str, object]] = {}
    for episode in episodes:
        episode_id = str(
            episode.get("episode_id") or episode.get("id") or ""
        )
        keywords = {
            str(item)
            for item in episode.get("matched_keywords") or []
            if str(item)
        }
        for match in episode.get("matches") or []:
            if not isinstance(match, dict) or not match.get("turn_id"):
                continue
            turn_id = str(match["turn_id"])
            state = turns.setdefault(
                turn_id,
                {
                    "turn_id": turn_id,
                    "episode_ids": [],
                    "matched_keywords": set(),
                    "match_ids": set(),
                    "last_activity_at": 0.0,
                    "search_score": 0.0,
                },
            )
            episode_ids = state["episode_ids"]
            if isinstance(episode_ids, list) and episode_id and episode_id not in episode_ids:
                episode_ids.append(episode_id)
            matched_keywords = state["matched_keywords"]
            if isinstance(matched_keywords, set):
                matched_keywords.update(keywords)
            match_ids = state["match_ids"]
            if isinstance(match_ids, set) and match.get("id") is not None:
                match_ids.add(int(match["id"]))
            state["last_activity_at"] = max(
                float(state["last_activity_at"]),
                float(match.get("created_at") or 0),
            )
            state["search_score"] = max(
                float(state["search_score"]),
                float(episode.get("search_score") or 0),
            )
    results: list[dict[str, object]] = []
    for state in turns.values():
        results.append(
            {
                **state,
                "matched_keywords": sorted(state["matched_keywords"]),
                "match_ids": sorted(state["match_ids"]),
            }
        )
    return results


def _recall_rank_key(item: dict[str, object]) -> tuple[int, int, float, float, str]:
    keyword_count = int(
        item.get("keyword_match_count")
        or len(item.get("matched_keywords") or [])
    )
    recent = bool(item.get("is_recent"))
    multiple = keyword_count >= 2
    bucket = (
        5
        if recent and multiple
        else 4
        if recent and keyword_count
        else 3
        if multiple
        else 2
        if recent
        else 1
        if keyword_count
        else 0
    )
    return (
        bucket,
        keyword_count,
        float(item.get("search_score") or 0),
        float(item.get("last_activity_at") or 0),
        str(item.get("turn_id") or item.get("episode_id") or item.get("id") or ""),
    )


def _rank_recall_items(
    items: list[dict[str, object]],
) -> list[dict[str, object]]:
    return sorted(items, key=_recall_rank_key, reverse=True)


def _recalled_turn_context(
    store: Store,
    turns: object,
    token_budget: int = RECALLED_TURN_CONTEXT_TOKENS,
    *,
    exclude_turn_ids: set[str] | None = None,
) -> str:
    if not isinstance(turns, list) or token_budget <= 0:
        return ""
    excluded = exclude_turn_ids or set()
    selected = _rank_recall_items(
        [
            item
            for item in turns
            if isinstance(item, dict)
            and str(item.get("turn_id") or "")
            and str(item.get("turn_id")) not in excluded
        ]
    )[:RECALLED_TURN_LIMIT]
    turn_ids = [str(item["turn_id"]) for item in selected]
    messages = store.conversation_messages_for_turns(turn_ids)
    by_turn: dict[str, list[dict[str, object]]] = {}
    for message in messages:
        by_turn.setdefault(str(message["turn_id"]), []).append(message)
    blocks: list[str] = []
    used = 0
    for item in selected:
        turn_id = str(item["turn_id"])
        lines = [f"[recalled turn={turn_id}]"]
        for message in by_turn.get(turn_id, []):
            role = "OWNER" if message["role"] == "user" else "MOMOI"
            delivery = str(message.get("delivery_state") or "")
            suffix = (
                f" delivery={delivery}"
                if role == "MOMOI" and delivery != "delivered"
                else ""
            )
            lines.append(
                f"{role} at={message.get('timestamp') or '?'}{suffix}: "
                f"{str(message.get('content') or '').strip()}"
            )
        block = "\n".join(lines)
        size = estimate_tokens(block)
        if blocks and used + size > token_budget:
            break
        if not blocks and size > token_budget:
            block = truncate_tokens(block, token_budget)
            size = estimate_tokens(block)
        blocks.append(block)
        used += size
    return "\n\n".join(blocks)


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


def _reminder_lines(items: object) -> str:
    if not isinstance(items, list):
        return ""
    lines = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        when = item.get("fire_timestamp")
        if not when and item.get("fire_at") is not None:
            when = context_timestamp(item["fire_at"])
        fields = [f"id={item['id']}"]
        if when:
            fields.append(f"at={when}")
        if item.get("schedule") not in (None, "", [], {}):
            fields.append(
                "schedule="
                + truncate_tokens(
                    json.dumps(item["schedule"], ensure_ascii=False, separators=(",", ":")),
                    80,
                )
            )
        if item.get("text"):
            fields.append(f"text={truncate_tokens(str(item['text']), 120)}")
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
    if name in {"memory_search", "conversation_search", "thinking_search"}:
        keep = ("query", "limit")
    elif name in {"conversation_read", "thinking_read"}:
        keep = ("episode_id", "message_id", "content_offset", "before_ordinal")
    elif name in {"goal_create", "goal_update", "goal_finish", "goal_cancel"}:
        keep = ("goal_id", "title", "status", "next_action")
    elif name in {"reminder_create", "reminder_cancel"}:
        keep = ("reminder_id", "text", "fire_at")
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
    for key in ("state", "status", "count", "next_cursor"):
        item = value.get(key)
        if item not in (None, "", [], {}):
            parts.append(f"{key}={item}")
    nested = value.get("goal") or value.get("reminder") or value.get("memory")
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
                for mutation_name in ("memories", "forgotten_memories", "goals", "reminders"):
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
    reminder = projected.get("reminder")
    if isinstance(reminder, dict):
        projected["reminder"] = {
            key: copy.deepcopy(reminder[key])
            for key in ("id", "text", "status", "fire_at", "schedule")
            if reminder.get(key) not in (None, "", [], {})
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
    recent_turn_records: dict[str, object] = {
        "version": 1,
        "turns": raw_recent_turns,
    }
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
    recent_turn_ids = {
        str(item.get("turn_id") or "")
        for item in recent_turn_records.get("turns") or []
        if isinstance(item, dict) and item.get("turn_id")
    }
    compact_recent_conversation = assemble_compact_recent_conversation(
        store,
        min(4, recent_turns),
        min(1600, max(400, raw_token_budget // 3)),
        recent_before_timestamp,
    )
    return {
        "recent_turn_base": recent_turn_base,
        "recent_turn_append": recent_turn_append,
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
            "Recalled daily reflections (top-k; fallible and lower authority):\n"
            + _memory_lines(retrieval.get("reflection_memories"))
            if retrieval.get("reflection_memories")
            else ""
        ),
        "recalled_turns": _recalled_turn_context(
            store,
            retrieval.get("recalled_turns"),
            RECALLED_TURN_CONTEXT_TOKENS,
            exclude_turn_ids=recent_turn_ids,
        ),
        "goals": _goal_lines(retrieval.get("goals")),
        "reminders": _reminder_lines(retrieval.get("reminders")),
    }


def recall_episode_context_parts(
    store: Store,
    query: str,
    max_results: int,
    summary_token_budget: int,
    raw_token_budget: int,
    *,
    skip_empty_webhook: bool = False,
    exclude_turn_ids: set[str] | None = None,
) -> tuple[str, str]:
    query = query.strip()
    if not query:
        return "", ""
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
    episodes = _rank_recall_items(episodes)
    recalled_turns = _collect_recalled_turns(episodes)
    for item in recalled_turns:
        item["is_recent"] = str(item["turn_id"]) in recent_ids
        item["keyword_match_count"] = len(
            item.get("matched_keywords") or []
        )
    recalled_turns = _rank_recall_items(recalled_turns)
    return (
        _episode_context(
            store,
            episodes,
            summary_token_budget,
            raw_token_budget,
            skip_empty_webhook=skip_empty_webhook,
        ),
        _recalled_turn_context(
            store,
            recalled_turns,
            min(raw_token_budget, RECALLED_TURN_CONTEXT_TOKENS),
            exclude_turn_ids=exclude_turn_ids,
        ),
    )


def recall_episode_context(
    store: Store,
    query: str,
    max_results: int,
    summary_token_budget: int,
    raw_token_budget: int,
    *,
    skip_empty_webhook: bool = False,
) -> str:
    episodes, _ = recall_episode_context_parts(
        store,
        query,
        max_results,
        summary_token_budget,
        raw_token_budget,
        skip_empty_webhook=skip_empty_webhook,
    )
    return episodes
