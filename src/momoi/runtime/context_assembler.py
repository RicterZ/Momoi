import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..config import AppConfig
from ..storage import Store, estimate_tokens, truncate_tokens
from .context_planner import is_light_social_plan, is_social_plan


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
) -> list[dict[str, object]]:
    if max_results <= 0 or token_budget <= 0:
        return []
    candidates: list[tuple[str, list[dict[str, object]]]] = []
    for unit in units:
        seen: dict[object, dict[str, object]] = {}
        rows: list[dict[str, object]] = []
        for query in unit["recall_queries"]:
            for row in search(str(query), max_results):
                key = identity(row)
                if key in seen:
                    _merge_matches(seen[key], row)
                else:
                    seen[key] = row
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
                _merge_matches(existing, row)
                continue
            if index > 0 and len(selected) >= max_results:
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
    social_plan = is_social_plan(plan)
    task_units = [] if social_plan else units

    core_confirmed = [] if social_plan else _selected_by_unit(
        [{"id": "core", "recall_queries": [""]}],
        lambda query, limit: store.search_memories(query, limit, include_core=True),
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
    core_ids = {item["id"] for item in core_confirmed}
    core_tokens = sum(
        estimate_tokens(f"[{item['kind']}:{item['key']}] {item['content']}")
        for item in core_confirmed
    )
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
        max(0, config.memory_tokens - core_tokens),
    )
    confirmed = [*core_confirmed, *(item for item in confirmed if item["id"] not in core_ids)]
    reflection_limit = (
        max(1, config.memory_results // 2) if config.memory_results else 0
    )
    reflection = _selected_by_unit(
        task_units,
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
    recalled_episodes = _selected_by_unit(
        units,
        store.search_episodes,
        lambda row: row["id"],
        lambda row: truncate_tokens(
            _episode_search_text(row),
            max(
                1,
                config.summary_tokens // max(1, config.summary_results, len(units)),
            ),
        ),
        lambda row: {
            "episode_id": row["id"],
            "relation": "recalled",
            "is_new": False,
            "matches": row.get("matches", []),
            "scene_only": social_plan,
        },
        config.summary_results,
        config.summary_tokens,
    )
    agenda_budget = config.memory_tokens // 2
    goals = _selected_by_unit(
        task_units,
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
        task_units,
        store.search_reminders,
        lambda row: row["id"],
        lambda row: str(row["text"]),
        lambda row: {
            name: row.get(name) for name in ("id", "text", "fire_at", "schedule")
        },
        config.memory_results,
        agenda_budget,
    )
    conflicts = _selected_by_unit(
        task_units,
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
    episodes: dict[str, dict[str, object]] = {}
    new_episodes: list[dict[str, object]] = []
    episode_limit = max(config.summary_results, len(units))
    light_social = is_light_social_plan(plan)
    unit_by_id = {
        str(unit["id"]): unit
        for unit in units
        if isinstance(unit, dict) and unit.get("id")
    }
    for binding in bindings:
        item = {
            "episode_id": binding["episode_id"],
            "relation": binding["relation"],
            "unit_ids": list(binding["unit_ids"]),
            "is_new": binding["is_new"],
            "scene_only": social_plan,
        }
        if binding["is_new"]:
            new_episodes.append(item)
        elif light_social and all(
            str(unit_id) in unit_by_id
            and unit_by_id[str(unit_id)].get("speech_act")
            in {
                "emotional_share",
                "casual_share",
                "banter",
                "acknowledgment",
                "closing",
            }
            for unit_id in binding["unit_ids"]
        ):
            continue
        elif len(episodes) < episode_limit:
            episodes[str(binding["episode_id"])] = item
    for recalled in recalled_episodes:
        episode_id = str(recalled["episode_id"])
        existing = episodes.get(episode_id)
        if existing is not None:
            existing_units = existing["unit_ids"]
            for unit_id in recalled["unit_ids"]:
                if unit_id not in existing_units:
                    existing_units.append(unit_id)
            existing["matches"] = recalled.get("matches", [])
            existing["scene_only"] = existing.get("scene_only") or recalled.get(
                "scene_only", False
            )
        elif len(episodes) < episode_limit:
            episodes[episode_id] = recalled
    return {
        "version": 2,
        "episodes": [*episodes.values(), *new_episodes],
        "confirmed_memories": confirmed,
        "reflection_memories": reflection,
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
        f"- [units={_supports(item)}] [{item['kind']}:{item['key']}] {item['content']}"
        for item in items
    )


def _episode_search_text(episode: dict[str, object]) -> str:
    return " ".join(
        str(episode.get(name) or "")
        for name in (
            "title",
            "working_summary",
            "summary",
            "topics",
            "entities",
            "open_loops",
            "matches",
        )
    )


def _scene_summary(episode: dict[str, object]) -> str:
    claims = episode.get("working_summary_claims")
    if not isinstance(claims, list):
        return ""
    return "\n".join(
        str(claim["quote"])
        for claim in claims
        if isinstance(claim, dict)
        and claim.get("role") == "user"
        and isinstance(claim.get("quote"), str)
        and claim["quote"].strip()
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


def _message_role(message: dict[str, object]) -> str:
    role = str(message.get("role") or "").upper()
    state = str(message.get("delivery_state") or "")
    if state == "uncertain":
        return f"{role} delivery=uncertain"
    if state == "internal":
        return f"{role} visibility=internal"
    return role


def _episode_context(
    store: Store,
    episodes: object,
    summary_token_budget: int,
    raw_token_budget: int,
    exclude_message_ids: set[int] | None = None,
) -> str:
    if not isinstance(episodes, list):
        return ""
    existing = [
        item
        for item in episodes
        if not item.get("is_new") and store.episode(str(item["episode_id"]))
    ]
    if not existing or (summary_token_budget <= 0 and raw_token_budget <= 0):
        return ""
    per_summary = max(1, summary_token_budget // len(existing))
    per_raw_tail = max(1, raw_token_budget // len(existing))
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
        summary = (
            _scene_summary(episode)
            if selected.get("scene_only") is True
            else str(episode["summary"] or episode["working_summary"])
        )
        if summary and summary_token_budget > 0:
            lines.append(f"summary: {truncate_tokens(summary, per_summary)}")
        if episode["topics"]:
            lines.append(f"topics: {json.dumps(episode['topics'], ensure_ascii=False)}")
        if episode["open_loops"]:
            lines.append(
                f"open_loops: {json.dumps(episode['open_loops'], ensure_ascii=False)}"
            )
        remaining_raw = per_raw_tail if raw_token_budget > 0 else 0
        scene_only = selected.get("scene_only") is True
        matched_ids: set[int] = set()
        matched_lines: list[str] = []
        for message in selected.get("matches", []):
            if not isinstance(message, dict) or remaining_raw <= 0:
                continue
            if scene_only and message.get("role") != "user":
                continue
            if isinstance(message.get("id"), int) and int(message["id"]) in (
                exclude_message_ids or set()
            ):
                continue
            prefix = (
                f"  [{_message_role(message)} "
                f"turn={message.get('turn_id')} ordinal={message.get('ordinal')}] "
            )
            prefix_tokens = estimate_tokens(prefix)
            if prefix_tokens >= remaining_raw:
                break
            content = truncate_tokens(
                str(message.get("content") or ""), remaining_raw - prefix_tokens
            )
            line = prefix + content
            matched_lines.append(line)
            remaining_raw -= estimate_tokens(line)
            if isinstance(message.get("id"), int):
                matched_ids.add(int(message["id"]))
        if matched_lines:
            lines.append("matched_raw:")
            lines.extend(matched_lines)
        messages = (
            store.episode_messages(
                str(episode["id"]),
                remaining_raw,
                after_ordinal=int(episode["summarized_through_ordinal"]),
                exclude_message_ids=exclude_message_ids,
            )
            if remaining_raw > 0
            else []
        )
        messages = [
            message for message in messages if int(message["id"]) not in matched_ids
        ]
        if scene_only:
            messages = [message for message in messages if message["role"] == "user"]
        if messages:
            lines.append("raw_tail:")
            lines.extend(
                f"  [{_message_role(message)} turn={message['turn_id']} "
                f"ordinal={message['ordinal']}] {message['content']}"
                for message in messages
            )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def assemble_main_context(
    store: Store,
    retrieval: dict[str, object],
    summary_token_budget: int,
    raw_token_budget: int,
    recent_turns: int = 0,
) -> dict[str, str]:
    recent_messages = store.recent_conversation_messages(
        recent_turns, raw_token_budget
    )
    recent = "\n".join(
        f"[{_message_role(message)} turn={message['turn_id']}] "
        f"{message['content']}"
        for message in recent_messages
    )
    recent_ids = {int(message["id"]) for message in recent_messages}
    remaining_raw = (
        max(0, raw_token_budget - estimate_tokens(recent))
        if recent
        else raw_token_budget
    )
    reflection = _memory_lines(retrieval.get("reflection_memories"))
    if reflection:
        reflection = (
            "These are fallible, lower-authority daily learnings.\n" + reflection
        )
    return {
        "recent_conversation": recent,
        "episodes": _episode_context(
            store,
            retrieval.get("episodes"),
            summary_token_budget,
            remaining_raw,
            recent_ids,
        ),
        "confirmed_memories": _memory_lines(retrieval.get("confirmed_memories")),
        "reflection_memories": reflection,
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
    )
    return _episode_context(store, episodes, summary_token_budget, raw_token_budget)
