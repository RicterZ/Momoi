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
        records.append((float(row["created_at"]), 1, int(row["id"]), text))
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
        records.append((float(row["created_at"]), 0, int(row["sequence"]), text))
    records.sort(key=lambda item: item[:3])
    return [{"role": "user", "content": "<conversation_history>\n"
             + "\n".join(item[3] for item in records) + "\n</conversation_history>"}]
