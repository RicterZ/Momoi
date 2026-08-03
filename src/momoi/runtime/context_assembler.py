import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..config import AppConfig
from ..storage import Store, estimate_tokens


def _selected_by_unit(
    units: list[dict[str, Any]],
    search: Callable[[str, int], list[dict[str, object]]],
    identity: Callable[[dict[str, object]], object],
    render: Callable[[dict[str, object]], str],
    snapshot: Callable[[dict[str, object]], dict[str, object]],
    max_results: int,
    token_budget: int,
) -> list[dict[str, object]]:
    if max_results <= 0 or token_budget <= 0:
        return []
    candidates: list[tuple[str, list[dict[str, object]]]] = []
    for unit in units:
        seen: set[object] = set()
        rows: list[dict[str, object]] = []
        for query in unit["recall_queries"]:
            for row in search(str(query), max_results):
                key = identity(row)
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
        candidates.append((str(unit["id"]), rows))

    selected: dict[object, dict[str, object]] = {}
    used = 0
    rounds = max((len(rows) for _, rows in candidates), default=0)
    for index in range(rounds):
        for unit_id, rows in candidates:
            if index >= len(rows):
                continue
            row = rows[index]
            key = identity(row)
            existing = selected.get(key)
            if existing is not None:
                unit_ids = existing["unit_ids"]
                if unit_id not in unit_ids:
                    unit_ids.append(unit_id)
                continue
            if len(selected) >= max_results:
                continue
            size = estimate_tokens(render(row))
            if used + size > token_budget:
                continue
            item = snapshot(row)
            item["unit_ids"] = [unit_id]
            selected[key] = item
            used += size
    return list(selected.values())


def build_plan_retrieval(
    store: Store, plan: dict[str, object], config: AppConfig
) -> dict[str, object]:
    units = plan.get("intent_units")
    bindings = plan.get("episode_bindings")
    if not isinstance(units, list) or not isinstance(bindings, list):
        raise RuntimeError("context plan has invalid retrieval inputs")

    confirmed = _selected_by_unit(
        units,
        lambda query, limit: store.search_memories(query, limit),
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
    legacy = _selected_by_unit(
        units,
        lambda query, limit: store.search_conversation_summaries(
            query, limit, include_latest=False
        ),
        lambda row: row["id"],
        lambda row: str(row["content"]),
        lambda row: {
            "id": row["id"],
            "start_message_id": row["start_message_id"],
            "end_message_id": row["end_message_id"],
            "content": row["content"],
        },
        config.summary_results,
        config.summary_tokens,
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
                "retry_at",
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
            for name in ("id", "text", "fire_at", "schedule")
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
    episodes = [
        {
            "episode_id": binding["episode_id"],
            "relation": binding["relation"],
            "unit_ids": binding["unit_ids"],
            "is_new": binding["is_new"],
        }
        for binding in bindings
    ]
    return {
        "version": 1,
        "episodes": episodes,
        "confirmed_memories": confirmed,
        "reflection_memories": reflection,
        "legacy_summaries": legacy,
        "goals": goals,
        "reminders": reminders,
        "memory_conflicts": conflicts,
        "uncertainty": plan.get("uncertainty", []),
    }


def _supports(item: dict[str, object]) -> str:
    return ",".join(str(value) for value in item.get("unit_ids", []))


def _memory_lines(items: object) -> str:
    if not isinstance(items, list):
        return ""
    return "\n".join(
        f"- [units={_supports(item)}] [{item['kind']}:{item['key']}] "
        f"{item['content']}"
        for item in items
    )


def _legacy_lines(items: object) -> str:
    if not isinstance(items, list):
        return ""
    return "\n".join(
        f"- [units={_supports(item)} legacy_messages="
        f"{item['start_message_id']}-{item['end_message_id']}] {item['content']}"
        for item in items
    )


def _goal_lines(items: object) -> str:
    if not isinstance(items, list):
        return ""
    return "\n".join(
        f"- [units={_supports(item)}] id={item['id']} status={item['status']} "
        f"title={item['title']} next_action={item.get('next_action') or 'none'} "
        f"waiting_for={item.get('waiting_for') or 'none'} "
        f"latest_result={item.get('latest_result') or 'none'} "
        f"next_review_at={item.get('next_review_at') or 'none'}"
        for item in items
    )


def _reminder_lines(items: object) -> str:
    if not isinstance(items, list):
        return ""
    lines = []
    for item in items:
        fire_at = item.get("fire_at")
        when = (
            datetime.fromtimestamp(float(fire_at)).astimezone().isoformat()
            if fire_at is not None
            else "none"
        )
        lines.append(
            f"- [units={_supports(item)}] id={item['id']} fire_at={when} "
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


def _episode_context(
    store: Store, episodes: object, token_budget: int
) -> str:
    if not isinstance(episodes, list):
        return ""
    existing = [
        item
        for item in episodes
        if not item.get("is_new") and store.episode(str(item["episode_id"]))
    ]
    if not existing or token_budget <= 0:
        return ""
    per_episode = max(1, token_budget // len(existing))
    sections: list[str] = []
    for selected in existing:
        episode = store.episode(str(selected["episode_id"]))
        if episode is None:
            continue
        lines = [
            f"[episode id={episode['id']} units={_supports(selected)} "
            f"relation={selected['relation']} status={episode['status']}]",
            f"title: {episode['title']}",
        ]
        if episode["working_summary"] or episode["summary"]:
            lines.append(
                f"summary: {episode['working_summary'] or episode['summary']}"
            )
        if episode["topics"]:
            lines.append(
                f"topics: {json.dumps(episode['topics'], ensure_ascii=False)}"
            )
        if episode["open_loops"]:
            lines.append(
                f"open_loops: {json.dumps(episode['open_loops'], ensure_ascii=False)}"
            )
        messages = store.episode_messages(
            str(episode["id"]),
            per_episode,
            after_ordinal=int(episode["summarized_through_ordinal"]),
        )
        if messages:
            lines.append("raw_tail:")
            lines.extend(
                f"  [{message['role'].upper()} turn={message['turn_id']} "
                f"ordinal={message['ordinal']}] {message['content']}"
                for message in messages
            )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def assemble_main_context(
    store: Store, retrieval: dict[str, object], raw_token_budget: int
) -> dict[str, str]:
    reflection = _memory_lines(retrieval.get("reflection_memories"))
    if reflection:
        reflection = (
            "These are fallible, lower-authority daily learnings.\n" + reflection
        )
    return {
        "episodes": _episode_context(
            store, retrieval.get("episodes"), raw_token_budget
        ),
        "legacy_conversation": _legacy_lines(
            retrieval.get("legacy_summaries")
        ),
        "confirmed_memories": _memory_lines(
            retrieval.get("confirmed_memories")
        ),
        "reflection_memories": reflection,
        "goals": _goal_lines(retrieval.get("goals")),
        "reminders": _reminder_lines(retrieval.get("reminders")),
        "memory_conflicts": _conflict_lines(
            retrieval.get("memory_conflicts")
        ),
    }
