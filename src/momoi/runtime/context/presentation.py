import json
from collections.abc import Mapping, Sequence
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.sax.saxutils import escape, quoteattr


def _attributes(values: dict[str, object]) -> str:
    return "".join(
        f" {key}={quoteattr(str(value))}"
        for key, value in values.items()
        if value is not None and value != ""
    )


def turn_label_ranges(values: list[str]) -> str:
    numbers = sorted(
        {
            int(value[2:])
            for value in values
            if value.startswith("T-") and value[2:].isdigit()
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
        ranges.append(f"T-{start}" if start == previous else f"T-{start}..T-{previous}")
        start = previous = number
    if start:
        ranges.append(f"T-{start}" if start == previous else f"T-{start}..T-{previous}")
    return ",".join(ranges)


def recent_episode_lines(
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
        attributes = _attributes({
            "id": episode["id"], "turns": episode_labels,
            "last_activity": episode.get("last_activity_timestamp"),
            "summary_state": episode.get("summary_state") if episode.get("summary_state") != "ready" else None,
        })
        summary = str(episode.get("narrative_summary") or "").strip()
        body = f"<title>{escape(str(episode['title'])[:120])}</title>"
        if episode.get("summary_state") == "partial":
            summary = "此话题的早期对话已移出 transcript，后续对话仍在近期记录中；需要早期细节时用 episode_read 读取。"
        elif episode.get("summary_state") == "pending":
            summary = "话题摘要尚未生成，原始对话仍可通过 episode_read 读取。"
        body += f"<summary>{escape(summary)}</summary>"
        lines.append(f"<episode{attributes}>{body}</episode>")
    return "\n".join(lines)


def due_goal_lines(
    goal: Mapping[str, object], *, scheduled_review_at: str
) -> str:
    attributes = {
        key: str(goal[key])
        for key in ("id", "status")
        if goal.get(key) not in (None, "")
    }
    if scheduled_review_at:
        attributes["review_at"] = scheduled_review_at
    record = Element("goal", attributes)
    for tag, field in (
        ("title", "title"),
        ("success_criteria", "success_criteria"),
    ):
        SubElement(record, tag).text = str(goal.get(field) or "")

    for tag, field in (
        ("next_action", "next_action"),
        ("waiting_for", "waiting_for"),
        ("result", "latest_result"),
    ):
        if goal.get(field) not in (None, ""):
            SubElement(record, tag).text = str(goal[field])

    schedule = goal.get("schedule")
    if isinstance(schedule, Mapping):
        schedule_attributes = (
            {"kind": str(schedule["kind"])}
            if schedule.get("kind") not in (None, "")
            else {}
        )
        schedule_node = SubElement(
            record,
            "schedule",
            schedule_attributes,
        )
        if schedule.get("kind") == "daily":
            times = schedule.get("times")
            if isinstance(times, Sequence) and not isinstance(times, (str, bytes)):
                for value in times:
                    SubElement(schedule_node, "time").text = str(value)
        elif schedule.get("every_seconds") is not None:
            schedule_node.set("every_seconds", str(schedule["every_seconds"]))
    return tostring(record, encoding="unicode")


def heartbeat_self_state_lines(value: str = "{}", *, current_time: str = "") -> str:
    try:
        state = json.loads(value)
    except (TypeError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    lines: list[str] = [f"<time now={quoteattr(current_time)} />"] if current_time else []
    mood = state.get("mood")
    if isinstance(mood, dict):
        attributes = _attributes({key: mood.get(key) for key in (
            "state", "intensity", "age_minutes", "updated_at",
        )})
        lines.append(f"<mood{attributes}><cause>{escape(str(mood.get('cause') or ''))}</cause></mood>")
    if state.get("last_heartbeat_at"):
        lines.append(f'<heartbeat at={quoteattr(str(state["last_heartbeat_at"]))} />')
    return "\n".join(lines)


def recall_context_lines(
    values: list[dict[str, object]],
) -> str:
    lines: list[str] = []
    for value in values:
        turn_id = str(value.get("turn_id") or "")
        queries = [str(item) for item in value.get("queries") or []]
        if not turn_id or not queries:
            continue
        body = "".join(f"<query>{escape(query)}</query>" for query in queries)
        lines.append(f"<recall turn={quoteattr(turn_id)}>{body}</recall>")
    return "\n".join(lines)
