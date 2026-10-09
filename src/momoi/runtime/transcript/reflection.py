"""Text-only daily evidence, without replaying historical agent instructions."""
import json
from xml.sax.saxutils import escape, quoteattr

from ...storage.episode.execution_evidence import bounded_result, historical_result
from ...storage.conversation.transcripts import TRANSCRIPT_PROTOCOL_TOOLS

BACKGROUND_TOOLS = {
    "recall", "memory_search", "episode_search", "episode_read", "read_tool_result",
    "thinking_search", "thinking_read",
    "weekly_reflection_finish", "reflection_finish", "episode_summary_finish", "episode_relation_finish",
    "episode_classify_turns", "episode_consolidation_finish", "current_state_finish",
    "memory_maintenance_finish", "memory_operation_finish",
}


def reflection_transcript(store, rows, window):
    records = []
    for row in rows:
        role = str(row["role"])
        delivery = str(row.get("delivery_state") or "unknown")
        text = (
            f'<message id={quoteattr(str(row["id"]))} speaker={quoteattr(role)} '
            f'time={quoteattr(store.context_timestamp(float(row["created_at"])))} '
            f'delivery={quoteattr(delivery)}>{escape(str(row["content"]))}</message>'
        )
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
        result = bounded_result(historical_result(payload.get("result", {}), name))
        text = (
            f'<tool_result name={quoteattr(name)} '
            f'time={quoteattr(store.context_timestamp(float(row["created_at"])))} '
            f'turn={quoteattr(str(row["turn_id"]))} '
            f'call_id={quoteattr(str(payload.get("tool_call_id") or ""))}>'
            + escape(json.dumps({"ok": payload.get("ok"), "error": payload.get("error"),
                                 "result": result}, ensure_ascii=False))
            + '</tool_result>'
        )
        records.append((float(row["created_at"]), 0, int(row["sequence"]), text, str(row["turn_id"])))
    records.sort(key=lambda item: item[:3])
    # A Turn may belong to multiple topics. Keep its evidence once in a shared
    # group instead of duplicating it and inflating the apparent sample count.
    turn_topics = {}
    titles = {}
    turn_ids = list(dict.fromkeys(item[4] for item in records))
    for offset in range(0, len(turn_ids), 500):
        chunk = turn_ids[offset:offset + 500]
        for row in store._db.execute(
            "SELECT et.turn_id, e.id, e.title FROM episode_turns et "
            "JOIN conversation_episodes e ON e.id=et.episode_id "
            "WHERE et.relation='primary' AND et.turn_id IN ("
            + ",".join("?" for _ in chunk) + ") ORDER BY e.id", chunk,
        ).fetchall():
            turn_topics.setdefault(str(row["turn_id"]), []).append(str(row["id"]))
            titles[str(row["id"])] = str(row["title"])
    groups = {}
    for record in records:
        key = tuple(turn_topics.get(record[4], []))
        groups.setdefault(key, []).append(record[3])
    sections = []
    for topic_ids, evidence in groups.items():
        topics = "\n".join(
            f'<topic id={quoteattr(topic_id)} title={quoteattr(titles[topic_id])} />'
            for topic_id in topic_ids
        )
        kind = "shared" if len(topic_ids) > 1 else "topic" if topic_ids else "unassigned"
        sections.append(
            f'<topic_group kind={quoteattr(kind)}>\n'
            + (topics + "\n" if topics else "")
            + "\n".join(evidence) + "\n</topic_group>"
        )
    return [{"role": "user", "content": "<conversation_history>\n"
             + "\n".join(sections) + "\n</conversation_history>"}]
