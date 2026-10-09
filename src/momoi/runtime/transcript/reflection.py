"""Text-only daily evidence, without replaying historical agent instructions."""
import json

from ...storage.episode.episode_sql import runtime_archive_kind_sql
from ...storage.episode.execution_evidence import bounded_result, historical_result
from ...storage.conversation.transcripts import TRANSCRIPT_PROTOCOL_TOOLS

BACKGROUND_TOOLS = {
    "goal_create", "goal_update", "goal_finish", "goal_cancel", "goal_review",
    "recall", "memory_search", "episode_search", "episode_read", "read_tool_result",
    "thinking_search", "thinking_read",
    "weekly_reflection_finish", "reflection_finish", "episode_summary_finish", "episode_relation_finish",
    "episode_classify_turns", "episode_consolidation_finish", "current_state_finish",
    "memory_maintenance_finish", "memory_operation_finish",
}


def reflection_tool_result(payload, name):
    """Render outcomes, not the live tool's retry protocol or transport envelope."""
    value = historical_result(payload.get("result", {}), name)
    failed = payload.get("ok") is False
    error = payload.get("error")
    reference = None
    while isinstance(value, dict):
        failed = failed or value.get("ok") is False
        error = value.get("error") or error
        reference = value.get("result_ref") or reference
        if "result" not in value or set(value) - {"ok", "error", "result", "result_ref"}:
            break
        value = value["result"]
    if error == "invalid_tool_arguments" and isinstance(value, dict):
        # The original validation message already names the field and constraint.
        value = str(value.get("message") or error).removesuffix(
            " Correct arguments only; do not repeat completed actions."
        )
    elif isinstance(value, dict):
        value = {key: item for key, item in value.items()
                 if key not in {"ok", "error", "result_ref"}}
    if not isinstance(value, dict):
        value = {"message": value} if error else {"content": value}
    if error:
        value = {"error": error, **value}
    elif failed:
        value = {"ok": False, **value}
    reduced = bounded_result(value)
    if reference and (reduced != value or "[...truncated...]" in json.dumps(reduced)):
        reduced["result_ref"] = reference
    return "TOOL RESULT: " + json.dumps(reduced, ensure_ascii=False, separators=(",", ":"))


def reflection_transcript(store, rows, window):
    records = []
    for row in rows:
        role = str(row["role"])
        if role in {"goal", "heartbeat"}:
            continue
        delivery = str(row.get("delivery_state") or "unknown")
        label = role.upper()
        if role == "assistant" and delivery != "delivered":
            label += f" ({delivery})"
        text = f"{label}: {row['content']}"
        records.append((float(row["created_at"]), 1, int(row["id"]), text, str(row["turn_id"])))
    for row in store._db.execute(
        "SELECT turn_id, sequence, created_at, payload_json FROM turn_journal "
        "WHERE item_type='tool_result' AND created_at>=? AND created_at<? "
        "ORDER BY created_at, sequence", window,
    ).fetchall():
        try:
            payload = json.loads(row["payload_json"])
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        name = str(payload.get("name") or "")
        if not name or name in BACKGROUND_TOOLS or name in TRANSCRIPT_PROTOCOL_TOOLS:
            continue
        call = store._db.execute(
            "SELECT payload_json FROM turn_journal WHERE turn_id=? AND item_type='tool_call' "
            "AND sequence<? AND json_extract(payload_json, '$.tool_call_id')=? "
            "ORDER BY sequence DESC LIMIT 1",
            (row["turn_id"], row["sequence"], payload.get("tool_call_id")),
        ).fetchone()
        arguments = json.loads(call["payload_json"]).get("arguments") if call else None
        if arguments is None:
            rendered_arguments = "参数未记录"
        else:
            if name == "bash" and isinstance(arguments, dict) and len(arguments) == 1:
                arguments = arguments.get("command", arguments.get("cmd", arguments))
            rendered_arguments = json.dumps(bounded_result(arguments), ensure_ascii=False, separators=(",", ":"))
        text = f"TOOL CALL: {name}({rendered_arguments})\n" + reflection_tool_result(payload, name)
        records.append((float(row["created_at"]), 0, int(row["sequence"]), text, str(row["turn_id"])))
    records.sort(key=lambda item: item[:3])
    # A Turn may belong to multiple topics. Keep its evidence once in a shared
    # group instead of duplicating it and inflating the apparent sample count.
    turn_topics = {}
    titles = {}
    excluded_turns = set()
    turn_ids = list(dict.fromkeys(item[4] for item in records))
    for offset in range(0, len(turn_ids), 500):
        chunk = turn_ids[offset:offset + 500]
        for row in store._db.execute(
            f"SELECT et.turn_id, e.id, e.title, {runtime_archive_kind_sql('e')} AS archive_kind FROM episode_turns et "
            "JOIN conversation_episodes e ON e.id=et.episode_id "
            "WHERE et.relation='primary' AND et.turn_id IN ("
            + ",".join("?" for _ in chunk) + ") ORDER BY e.id", chunk,
        ).fetchall():
            if row["archive_kind"] in {"webhook", "goal", "heartbeat"}:
                excluded_turns.add(str(row["turn_id"]))
                continue
            turn_topics.setdefault(str(row["turn_id"]), []).append(str(row["id"]))
            titles[str(row["id"])] = str(row["title"])
    groups = {}
    for record in records:
        if record[4] in excluded_turns:
            continue
        key = tuple(turn_topics.get(record[4], []))
        groups.setdefault(key, []).append(record)
    sections = []
    for topic_ids, evidence in groups.items():
        title = " / ".join(titles[topic_id] for topic_id in topic_ids) or "尚未归类"
        if len(topic_ids) > 1:
            title = "共同材料：" + title
        stamp = store.context_timestamp(evidence[0][0])
        blocks = []
        previous_label = None
        for record in evidence:
            text = record[3]
            label, _, body = text.partition(": ")
            if record[1] == 1 and label == previous_label:
                blocks[-1] += "\n" + body
            else:
                blocks.append(text)
            previous_label = label if record[1] == 1 else None
        sections.append(f"## {title} | {stamp}\n\n" + "\n\n".join(blocks))
    return [{"role": "user", "content": "<conversation_history>\n"
             + "\n\n".join(sections) + "\n</conversation_history>"}]
