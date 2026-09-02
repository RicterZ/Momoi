import json
import logging

from ...observability.events import log_event
from ...storage import (
    REFLECTION_MEMORY_CAUTION,
    Store,
    estimate_tokens,
    format_reflection_memory,
    truncate_tokens,
)
from ...storage.episode_ranking import rank_recall_items
from ..agent.budget import SECTION_BUDGET_ALLOCATOR
from .retrieval import _merge_matches

logger = logging.getLogger(__name__)
RECENT_EXTERNAL_EVENT_LIMIT = 6
RECENT_EXTERNAL_EVENT_LOOKBACK_SECONDS = 6 * 3600
RECENT_EXTERNAL_EVENT_TOKEN_BUDGET = 1200


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
            f"E-{index} {store.context_timestamp(last_seen)} [{event['source']}]",
            f"  event: {event['event']}",
        ]
        if occurrences > 1:
            lines.append(
                f"  observations: {occurrences} since {store.context_timestamp(first_seen)}"
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
        if record.get("workflow_kind") != "webhook":
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
