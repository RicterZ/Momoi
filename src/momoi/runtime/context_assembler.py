import copy
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from ..config import AppConfig
from ..context_time import context_timestamp
from ..logging_context import log_event, safe_preview
from ..search import search_alternatives
from ..storage import Store, estimate_tokens, truncate_tokens
from .budget import SECTION_BUDGET_ALLOCATOR


_LEGACY_OWNER_HEADER = "# Current owner messages\n"
_DEFAULT_EPISODE_LOOKBACK_SECONDS = 30 * 24 * 60 * 60
logger = logging.getLogger(__name__)


def _query_alternatives(query: str) -> list[str]:
    alternatives: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[|｜\n]", str(query)):
        normalized = " ".join(part.strip().split())
        folded = normalized.casefold()
        if not normalized or folded in seen:
            continue
        seen.add(folded)
        alternatives.append(normalized[:500])
    return alternatives[:12]


def _search_or(
    query: str,
    search: Callable[[str, int], list[dict[str, object]]],
    identity: Callable[[dict[str, object]], object],
    max_results: int,
) -> list[dict[str, object]]:
    alternatives = _query_alternatives(query)
    if not alternatives:
        return []
    if len(alternatives) == 1:
        return search(alternatives[0], max_results)

    ranked: dict[
        object, tuple[dict[str, object], int, float, int]
    ] = {}
    for alternative in alternatives:
        for rank, row in enumerate(search(alternative, max_results)):
            key = identity(row)
            existing = ranked.get(key)
            if existing is None:
                ranked[key] = (row, 1, 1.0 / (rank + 1), len(ranked))
            else:
                existing_row, hits, score, first = existing
                _merge_matches(existing_row, row)
                ranked[key] = (
                    existing_row,
                    hits + 1,
                    score + 1.0 / (rank + 1),
                    first,
                )

    rows = list(ranked.values())
    rows.sort(key=lambda item: (-item[1], item[3], -item[2]))
    return [item[0] for item in rows[:max_results]]


def _historical_content(value: object) -> str:
    """Remove the legacy owner wrapper before showing persisted history."""
    text = str(value or "")
    return text.removeprefix(_LEGACY_OWNER_HEADER)


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


def _selected_by_unit(
    units: list[dict[str, Any]],
    search: Callable[[str, int], list[dict[str, object]]],
    identity: Callable[[dict[str, object]], object],
    render: Callable[[dict[str, object]], str],
    snapshot: Callable[[dict[str, object]], dict[str, object]],
    max_results: int,
    token_budget: int,
    *,
    expand_or: bool = True,
) -> list[dict[str, object]]:
    if max_results <= 0 or token_budget <= 0:
        return []
    candidates: list[tuple[str, list[dict[str, object]]]] = []
    for unit in units:
        seen: dict[object, dict[str, object]] = {}
        rows: list[dict[str, object]] = []
        for query in unit["recall_queries"]:
            found = (
                _search_or(str(query), search, identity, max_results)
                if expand_or
                else search(str(query), max_results)
            )
            for row in found:
                key = identity(row)
                if key in seen:
                    _merge_matches(seen[key], row)
                else:
                    seen[key] = row
                    rows.append(row)
        candidates.append((str(unit["id"]), rows))

    return SECTION_BUDGET_ALLOCATOR.select(
        candidates,
        identity,
        render,
        snapshot,
        _merge_matches,
        max_results,
        token_budget,
    )


def build_plan_retrieval(
    store: Store, plan: dict[str, object], config: AppConfig
) -> dict[str, object]:
    units = plan.get("intent_units")
    actions = plan.get("episode_actions", plan.get("episode_bindings"))
    if not isinstance(units, list) or not isinstance(actions, list):
        raise RuntimeError("context plan has invalid retrieval inputs")

    confirmed = _selected_by_unit(
        units,
        lambda query, limit: store.search_memories(
            query, limit, activation="recall"
        ),
        lambda row: row["id"],
        lambda row: f"[{row['kind']}:{row['key']}] {row['content']}",
        lambda row: {
            "id": row["id"],
            "kind": row["kind"],
            "key": row["key"],
            "content": row["content"],
        },
        config.memory_results,
        config.memory_tokens,
    )
    reflection_limit = (
        max(1, config.memory_results // 2) if config.memory_results else 0
    )
    reflection = _selected_by_unit(
        units,
        lambda query, limit: store.search_reflection_memories(query, limit),
        lambda row: row["id"],
        lambda row: f"[{row['kind']}:{row['key']}] {row['content']}",
        lambda row: {
            "id": row["id"],
            "kind": row["kind"],
            "key": row["key"],
            "content": row["content"],
            "confidence": row["confidence"],
        },
        reflection_limit,
        config.memory_tokens // 2,
    )
    episode_results = config.summary_results
    keyword_query = " | ".join(
        str(query)
        for unit in units
        if isinstance(unit, dict)
        for query in unit.get("recall_queries", [])
        if str(query).strip()
    )
    recalled_episodes = []
    if episode_results > 0 and config.summary_tokens > 0 and keyword_query:
        for row in store.search_episodes(
            keyword_query,
            episode_results,
            after=time.time() - _DEFAULT_EPISODE_LOOKBACK_SECONDS,
        ):
            recalled_episodes.append(
                {
                    "episode_id": row["id"],
                    "relation": "recalled",
                    "is_new": False,
                    "matches": row.get("matches", []),
                    "keyword_match_count": int(
                        row.get("keyword_match_count") or 0
                    ),
                    "last_activity_at": float(
                        row.get("last_activity_at") or 0
                    ),
                    "unit_ids": [
                        str(unit["id"])
                        for unit in units
                        if isinstance(unit, dict)
                        and any(
                            alternative
                            in {
                                keyword
                                for query in unit.get("recall_queries", [])
                                for keyword in search_alternatives(str(query))
                            }
                            for alternative in row.get(
                                "matched_keywords", []
                            )
                        )
                    ],
                }
            )
    recent_episodes = (
        store.list_recent_episodes(
            time.time() - config.recent_episode_hours * 3600
        )
        if config.recent_episode_hours > 0 and config.summary_tokens > 0
        else []
    )
    episodes_by_id: dict[str, dict[str, object]] = {
        str(item["episode_id"]): item for item in recalled_episodes
    }
    for episode in recent_episodes:
        episode_id = str(episode["id"])
        existing = episodes_by_id.get(episode_id)
        if existing is not None:
            existing["relation"] = "recent_recalled"
            existing["last_activity_at"] = max(
                float(existing.get("last_activity_at") or 0),
                float(episode.get("last_activity_at") or 0),
            )
            continue
        episodes_by_id[episode_id] = {
            "episode_id": episode_id,
            "relation": "recent",
            "is_new": False,
            "matches": [],
            "unit_ids": [],
            "keyword_match_count": 0,
            "last_activity_at": float(episode.get("last_activity_at") or 0),
        }
    def episode_priority(item: dict[str, object]) -> int:
        keyword_count = int(item.get("keyword_match_count") or 0)
        recent = item["relation"] in {"recent", "recent_recalled"}
        if recent and keyword_count > 1:
            return 4
        if keyword_count > 1:
            return 3
        if keyword_count > 0:
            return 2
        return 1 if recent else 0

    ordered_episodes = sorted(
        episodes_by_id.values(),
        key=lambda item: (
            episode_priority(item),
            int(item.get("keyword_match_count") or 0),
            float(item.get("last_activity_at") or 0),
            str(item["episode_id"]),
        ),
        reverse=True,
    )
    agenda_budget = config.memory_tokens // 2
    goals = _selected_by_unit(
        units,
        store.search_goals,
        lambda row: row["id"],
        lambda row: " ".join(
            str(row.get(name) or "")
            for name in ("title", "next_action", "waiting_for", "latest_result")
        ),
        lambda row: {
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
        },
        config.memory_results,
        agenda_budget,
    )
    reminders = _selected_by_unit(
        units,
        store.search_reminders,
        lambda row: row["id"],
        lambda row: str(row["text"]),
        lambda row: {
            name: row.get(name)
            for name in ("id", "text", "fire_at", "fire_timestamp", "schedule")
        },
        config.memory_results,
        agenda_budget,
    )
    conflicts = _selected_by_unit(
        units,
        store.search_memory_conflicts,
        lambda row: row["id"],
        lambda row: (
            f"{row['kind']} {row['key']} {row['existing_content']} "
            f"{row['candidate_content']}"
        ),
        lambda row: {
            name: row[name]
            for name in (
                "id",
                "kind",
                "key",
                "existing_content",
                "candidate_content",
            )
        },
        config.memory_results,
        config.memory_tokens // 2,
    )
    retrieval = {
        "version": 2,
        # Recent Episodes are included by time window; explicit recall queries add
        # up to the configured keyword result limit before the two sets are merged.
        "episodes": ordered_episodes,
        "confirmed_memories": confirmed,
        "owner_preferences": store.always_memory_context(),
        "recent_memories": store.recent_memory_context(
            max(100, config.memory_tokens // 8)
        ),
        "reflection_memories": reflection,
        "core_reflection_memories": store.core_reflection_memory_context(
            min(900, max(200, config.memory_tokens // 6))
        ),
        "goals": goals,
        "reminders": reminders,
        "memory_conflicts": conflicts,
        "uncertainty": plan.get("uncertainty", []),
    }
    log_event(
        logger,
        logging.INFO,
        "context_recall",
        stage="context_recall",
        queries=[
            {
                "unit": str(unit.get("id") or ""),
                "patterns": [
                    str(query) for query in unit.get("recall_queries", [])
                ],
            }
            for unit in units
            if isinstance(unit, dict) and unit.get("recall_queries")
        ],
        episodes=[
            {
                "id": item["episode_id"],
                "units": item["unit_ids"],
            }
            for item in recalled_episodes
        ],
        memories=[
            {
                "key": item["key"],
                "units": item["unit_ids"],
            }
            for item in confirmed
        ],
        reflections=[
            {
                "key": item["key"],
                "units": item["unit_ids"],
            }
            for item in reflection
        ],
        goals=[
            {"id": item["id"], "units": item["unit_ids"]}
            for item in goals
        ],
        reminders=[
            {"id": item["id"], "units": item["unit_ids"]}
            for item in reminders
        ],
        counts={
            "episodes": len(recalled_episodes),
            "memories": len(confirmed),
            "reflections": len(reflection),
            "goals": len(goals),
            "reminders": len(reminders),
            "conflicts": len(conflicts),
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
        f"- [units={_supports(item)}] [{item['kind']}:{item['key']}] {item['content']}"
        for item in items
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


def _goal_lines(items: object) -> str:
    if not isinstance(items, list):
        return ""
    return "\n".join(
        f"- [units={_supports(item)}] id={item['id']} status={item['status']} "
        f"title={item['title']} next_action={item.get('next_action') or 'none'} "
        f"waiting_for={item.get('waiting_for') or 'none'} "
        f"latest_result={item.get('latest_result') or 'none'} "
        f"next_review_at={item.get('next_review_timestamp') or 'none'} "
        f"retry_at={item.get('retry_timestamp') or 'none'}"
        for item in items
    )


def _reminder_lines(items: object) -> str:
    if not isinstance(items, list):
        return ""
    lines = []
    for item in items:
        when = item.get("fire_timestamp")
        if not when and item.get("fire_at") is not None:
            when = context_timestamp(item["fire_at"])
        lines.append(
            f"- [units={_supports(item)}] id={item['id']} fire_at={when or 'none'} "
            f"schedule={json.dumps(item.get('schedule'), ensure_ascii=False)} "
            f"text={item['text']}"
        )
    return "\n".join(lines)


def _conflict_lines(items: object) -> str:
    if not isinstance(items, list):
        return ""
    return "\n".join(
        f"- [units={_supports(item)}] conflict_id={item['id']} "
        f"[{item['kind']}:{item['key']}] current={item['existing_content']} "
        f"candidate={item['candidate_content']}"
        for item in items
    )


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


def _episode_context(
    store: Store,
    episodes: object,
    summary_token_budget: int,
    _raw_token_budget: int = 0,
    _exclude_message_ids: set[int] | None = None,
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
        lines = [
            f"[episode id={episode['id']} units={_supports(selected)} "
            f"relation={selected['relation']} status={episode['status']} "
            f"created={episode.get('created_timestamp') or 'unknown'} "
            f"updated={episode.get('updated_timestamp') or 'unknown'}]",
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
        f"{_historical_content(message['content'])}"
        for message in recent_messages
    )
    return recent, {int(message["id"]) for message in recent_messages}


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


def assemble_recent_turns(
    store: Store,
    turn_limit: int,
    token_budget: int,
    before_timestamp: float | None = None,
) -> tuple[dict[str, object], str]:
    if turn_limit <= 0 or token_budget <= 0:
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
        if not selected and size > token_budget:
            candidate = _compact_turn_record(record, token_budget)
            rendered = json.dumps(
                candidate,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            size = estimate_tokens(rendered)
        if selected and used + size > token_budget:
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


def project_recent_turns_for_planner(
    document: dict[str, object],
) -> dict[str, object]:
    """Keep planner-relevant history while preserving complete tool semantics."""
    projected_turns: list[dict[str, object]] = []
    turns = document.get("turns")
    for raw_turn in turns if isinstance(turns, list) else []:
        if not isinstance(raw_turn, dict):
            continue
        turn = {
            key: copy.deepcopy(value)
            for key, value in raw_turn.items()
            if key != "timeline"
        }
        final = turn.get("final")
        if isinstance(final, dict):
            final.pop("llm", None)
            final.pop("state", None)
            final.pop("channel", None)

        call_ids: dict[str, str] = {}

        def call_ref(value: object) -> str:
            raw = str(value or "")
            if not raw:
                return ""
            if raw not in call_ids:
                call_ids[raw] = f"t{len(call_ids) + 1}"
            return call_ids[raw]

        timeline: list[dict[str, object]] = []
        raw_timeline = raw_turn.get("timeline")
        for raw_item in raw_timeline if isinstance(raw_timeline, list) else []:
            if not isinstance(raw_item, dict):
                continue
            item_type = str(raw_item.get("type") or "")
            if item_type == "tool_call":
                timeline.append(
                    {
                        "type": item_type,
                        "call": call_ref(raw_item.get("tool_call_id")),
                        "name": copy.deepcopy(raw_item.get("name")),
                        "arguments": copy.deepcopy(raw_item.get("arguments")),
                        "timestamp": copy.deepcopy(raw_item.get("timestamp")),
                        "visibility": copy.deepcopy(raw_item.get("visibility")),
                    }
                )
                continue
            if item_type == "tool_result":
                timeline.append(
                    {
                        "type": item_type,
                        "call": call_ref(raw_item.get("tool_call_id")),
                        "name": copy.deepcopy(raw_item.get("name")),
                        "ok": copy.deepcopy(raw_item.get("ok")),
                        "error": copy.deepcopy(raw_item.get("error")),
                        "result": copy.deepcopy(raw_item.get("result")),
                        "timestamp": copy.deepcopy(raw_item.get("timestamp")),
                        "visibility": copy.deepcopy(raw_item.get("visibility")),
                    }
                )
                continue
            if item_type in {"owner_message", "assistant_message", "event"}:
                timeline.append(copy.deepcopy(raw_item))
                continue
            timeline.append(copy.deepcopy(raw_item))
        turn["timeline"] = timeline
        projected_turns.append(turn)
    return {
        "version": document.get("version", 1),
        "turns": projected_turns,
    }


def assemble_planner_recent_turns(
    store: Store,
    base_turns: int,
    append_turns: int,
    active_turns: int,
    token_budget: int,
    before_timestamp: float | None = None,
) -> tuple[dict[str, object], list[str]]:
    base_turns = max(1, int(base_turns))
    append_turns = max(1, int(append_turns))
    active_turns = max(1, int(active_turns))
    token_budget = max(1, int(token_budget))

    total = store.recent_turn_record_count(before_timestamp)
    phase = total % append_turns
    turn_limit = base_turns if phase == 0 else base_turns + phase
    raw_turns = store.recent_turn_records(turn_limit, before_timestamp)
    projected = project_recent_turns_for_planner(
        {"version": 1, "turns": raw_turns}
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
                {"version": 1, "turns": [compact]}
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
    return document, active_ids


def assemble_main_context(
    store: Store,
    retrieval: dict[str, object],
    summary_token_budget: int,
    raw_token_budget: int,
    recent_turns: int = 0,
    recent_before_timestamp: float | None = None,
) -> dict[str, str]:
    _recent_turn_records, recent_turns = assemble_recent_turns(
        store,
        recent_turns, raw_token_budget, recent_before_timestamp
    )
    reflection = _memory_lines(retrieval.get("reflection_memories"))
    if reflection:
        reflection = (
            "These are fallible, lower-authority daily learnings.\n" + reflection
        )
    return {
        "recent_turns": recent_turns,
        "episodes": _episode_context(
            store,
            retrieval.get("episodes"),
            summary_token_budget,
        ),
        "confirmed_memories": _memory_lines(retrieval.get("confirmed_memories")),
        "owner_preferences": str(retrieval.get("owner_preferences") or ""),
        "recent_memories": str(retrieval.get("recent_memories") or ""),
        "reflection_memories": reflection,
        "core_reflection_memories": str(
            retrieval.get("core_reflection_memories") or ""
        ),
        "goals": _goal_lines(retrieval.get("goals")),
        "reminders": _reminder_lines(retrieval.get("reminders")),
        "memory_conflicts": _conflict_lines(retrieval.get("memory_conflicts")),
    }


def recall_episode_context(
    store: Store,
    query: str,
    max_results: int,
    summary_token_budget: int,
    raw_token_budget: int,
) -> str:
    query = query.strip()
    if not query:
        return ""
    episodes = _selected_by_unit(
        [{"id": "query", "recall_queries": [query]}],
        store.search_episodes,
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
        },
        max_results,
        summary_token_budget,
        expand_or=False,
    )
    return _episode_context(store, episodes, summary_token_budget, raw_token_budget)
