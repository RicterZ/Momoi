import json


def turn_label_ranges(values: list[str]) -> str:
    numbers = sorted(
        {
            int(value[1:])
            for value in values
            if value.startswith("T") and value[1:].isdigit()
        }
    )
    ranges: list[str] = []
    start = previous = 0
    for number in numbers:
        if not start:
            start = previous = number
            continue
        if number == previous + 1:
            previous = number
            continue
        ranges.append(f"T{start}" if start == previous else f"T{start}-T{previous}")
        start = previous = number
    if start:
        ranges.append(f"T{start}" if start == previous else f"T{start}-T{previous}")
    return ",".join(ranges)


def episode_candidate_lines(
    items: list[dict[str, object]], labels: dict[str, str]
) -> str:
    lines: list[str] = []
    for episode in items:
        episode_labels = turn_label_ranges(
            [
                labels[turn_id]
                for turn_id in episode.get("turn_ids") or []
                if turn_id in labels
            ]
        )
        lines.append(
            f"- id={episode['id']} title={str(episode['title'])[:120]} "
            f"turns={episode_labels or '?'} "
            f"last_activity={episode.get('last_activity_timestamp') or '?'}"
        )
    return "\n".join(lines)


def heartbeat_topic_lines(items: list[dict[str, object]]) -> str:
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        fields: list[str] = []
        for key, limit in (("title", 120), ("updated_timestamp", 32)):
            value = item.get(key)
            if value not in (None, "", [], {}):
                fields.append(f"{key.removesuffix('_timestamp')}={str(value)[:limit]}")
        summary = str(item.get("summary") or "").strip()
        if summary:
            fields.append(f"summary={summary[:240]}")
        for key in ("topics", "entities", "open_loops"):
            values = item.get(key) or []
            if values:
                fields.append(f"{key}=" + ",".join(str(value) for value in values[:8]))
        if fields:
            lines.append("- " + " ".join(fields))
    return "\n".join(lines)


def heartbeat_activity_lines(items: list[dict[str, str]]) -> str:
    rendered = "\n".join(
        f"- at={item.get('at') or '?'} activity={str(item.get('text') or '').strip()}"
        for item in items
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    )
    return rendered or "(none)"


def heartbeat_self_state_lines(value: str) -> str:
    try:
        state = json.loads(value)
    except (TypeError, ValueError):
        return value
    if not isinstance(state, dict):
        return str(value)
    lines: list[str] = []
    mood = state.get("mood")
    if isinstance(mood, dict):
        fields = [
            f"state={mood.get('state') or 'unknown'}",
            f"intensity={mood.get('intensity') or 0}",
        ]
        for key in ("cause", "age_minutes", "updated_at"):
            if mood.get(key) not in (None, "", [], {}):
                fields.append(f"{key}={mood[key]}")
        lines.append("mood: " + " ".join(fields))
    activity = state.get("activity")
    if isinstance(activity, dict):
        fields = []
        for key in ("text", "result", "since"):
            value = str(activity.get(key) or "none").replace("\n", " ")
            fields.append(f"{key}={value}")
        lines.append("activity: " + " ".join(fields))
    if state.get("last_heartbeat_at"):
        lines.append(f"last heartbeat: {state['last_heartbeat_at']}")
    return "\n".join(lines) or "(none)"


def recall_context_lines(
    values: list[dict[str, object]],
) -> str:
    lines: list[str] = []
    for value in values:
        turn_id = str(value.get("turn_id") or "")
        queries = [str(item) for item in value.get("queries") or []]
        if not turn_id or not queries:
            continue
        lines.append(f"turn={turn_id} queries=" + " ; ".join(queries))
    return "\n".join(lines)

