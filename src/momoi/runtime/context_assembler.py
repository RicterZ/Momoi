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


_LEGACY_OWNER_HEADER = "# Current owner messages\n"
logger = logging.getLogger(__name__)


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


def build_plan_retrieval(
    store: Store,
    plan: dict[str, object],
    config: AppConfig,
) -> dict[str, object]:
    recent_episodes = (
        store.list_recent_episodes(
            time.time() - config.recent_episode_hours * 3600
        )
        if config.recent_episode_hours > 0 and config.summary_tokens > 0
        else []
    )
    episodes = [
        {
            "episode_id": str(episode["id"]),
            "relation": "recent",
            "is_new": False,
            "matches": [],
            "unit_ids": [],
            "last_activity_at": float(episode.get("last_activity_at") or 0),
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
    retrieval = {
        "version": 2,
        "episodes": episodes,
        "confirmed_memories": [],
        "owner_preferences": store.always_memory_context(),
        "recent_memories": store.recent_memory_context(
            max(100, config.memory_tokens // 8)
        ),
        "reflection_memories": [],
        "core_reflection_memories": store.core_reflection_memory_context(
            min(900, max(200, config.memory_tokens // 6))
        ),
        "goals": goals,
        "reminders": reminders,
        "memory_conflicts": [],
        "uncertainty": plan.get("uncertainty", []),
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


def _planner_message_text(value: object) -> str:
    text = str(value or "")
    return re.sub(
        r"(?m)^\d{4}-\d{2}-\d{2}T\S+\s+\[[^\]\n]+\]\s*",
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
    active_turn_ids = {
        str(turn.get("turn_id") or "")
        for turn in raw_turns[-active_turns:]
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
    return document, active_ids


def assemble_main_context(
    store: Store,
    retrieval: dict[str, object],
    summary_token_budget: int,
    raw_token_budget: int,
    recent_turns: int = 0,
    recent_before_timestamp: float | None = None,
) -> dict[str, str]:
    recent_turn_records, _recent_turns = assemble_recent_turns(
        store,
        recent_turns, raw_token_budget, recent_before_timestamp
    )
    compact_recent_turns = json.dumps(
        project_recent_turns_for_planner(recent_turn_records),
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    reflection = _memory_lines(retrieval.get("reflection_memories"))
    if reflection:
        reflection = (
            "These are fallible, lower-authority daily learnings.\n" + reflection
        )
    return {
        "recent_turns": compact_recent_turns,
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
        },
        _merge_matches,
        max_results,
        summary_token_budget,
    )
    return _episode_context(store, episodes, summary_token_budget, raw_token_budget)
