"""Daily reflection material: chat messages only, grouped by Episode."""
from html import escape

from ...storage.core.turn_workflow import turn_workflow_kind_sql
from ...storage.episode.episode_sql import runtime_archive_kind_sql

# Worker and background workflows keep their own transcripts; daily reflection
# only learns from what the owner and the assistant said in chat.
CHAT_WORKFLOWS = ("owner", "reply_followup", "channel_event")
RECALLED_MARK = "[此消息已被撤回]"


def _chunks(values, size=500):
    values = list(values)
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def _turn_topics(store, turn_ids):
    turn_topics = {}
    titles = {}
    excluded = set()
    for chunk in _chunks(dict.fromkeys(turn_id for turn_id in turn_ids if turn_id)):
        for row in store._db.execute(
            f"SELECT et.turn_id, e.id, e.title, {runtime_archive_kind_sql('e')} AS archive_kind "
            "FROM episode_turns et JOIN conversation_episodes e ON e.id=et.episode_id "
            "WHERE et.relation='primary' AND et.turn_id IN ("
            + ",".join("?" for _ in chunk) + ") ORDER BY e.id", chunk,
        ).fetchall():
            if row["archive_kind"] in {"webhook", "subagent_report", "goal", "heartbeat"}:
                excluded.add(str(row["turn_id"]))
                continue
            turn_topics.setdefault(str(row["turn_id"]), []).append(str(row["id"]))
            titles[str(row["id"])] = str(row["title"])
    return turn_topics, titles, excluded


def reflection_messages(store, rows):
    """Select the chat messages shared by the rendered history and evidence."""
    turn_ids = {str(row["turn_id"]) for row in rows if row.get("turn_id")}
    workflows = {}
    for chunk in _chunks(turn_ids):
        for row in store._db.execute(
            f"SELECT id, {turn_workflow_kind_sql('turns')} AS workflow FROM turns "
            f"WHERE id IN ({','.join('?' for _ in chunk)})", chunk,
        ).fetchall():
            workflows[str(row["id"])] = row["workflow"]
    _, _, archived = _turn_topics(store, turn_ids)
    selected = []
    for row in rows:
        role = str(row["role"])
        turn_id = str(row["turn_id"]) if row.get("turn_id") else None
        workflow = workflows.get(turn_id) if turn_id else None
        if turn_id in archived:
            continue
        if role == "user":
            if turn_id and workflow not in CHAT_WORKFLOWS:
                continue
        elif role == "assistant":
            if workflow not in CHAT_WORKFLOWS or row.get("delivery_state") != "delivered":
                continue
        else:
            continue
        recalled = bool(row.get("recalled"))
        selected.append({
            "id": int(row["id"]),
            "turn_id": turn_id,
            "role": role,
            "created_at": float(row["created_at"]),
            "recalled": recalled,
            "content": RECALLED_MARK if recalled else str(row["content"]),
        })
    return selected


def reflection_evidence(messages):
    """Quotable passages, one per message; recalled messages are not evidence."""
    passages = [item["content"] for item in messages if not item["recalled"]]
    owner = [item["content"] for item in messages
             if item["role"] == "user" and not item["recalled"]]
    return passages, owner


def reflection_transcript(store, messages):
    # A Turn may belong to multiple topics. Keep its messages once in a shared
    # group instead of duplicating them and inflating the apparent sample count.
    turn_topics, titles, _ = _turn_topics(store, [item["turn_id"] for item in messages])
    groups = {}
    for item in messages:
        groups.setdefault(tuple(turn_topics.get(item["turn_id"], [])), []).append(item)
    sections = []
    for topic_ids, items in groups.items():
        if topic_ids:
            title = " / ".join(titles[topic_id] for topic_id in topic_ids)
            header = ("共同材料：" if len(topic_ids) > 1 else "") + title
            header += " | episode_id=" + ",".join(topic_ids)
        else:
            header = "尚未归类"
        header += (f" | {store.context_timestamp(items[0]['created_at'])}"
                   f" ~ {store.context_timestamp(items[-1]['created_at'])}")
        blocks = []
        previous = None
        for item in items:
            body = escape(item["content"], quote=False)
            if item["role"] == previous:
                blocks[-1] += "\n" + body
            else:
                blocks.append(f"{item['role'].upper()}: {body}")
            previous = item["role"]
        sections.append(f"## {escape(header, quote=False)}\n\n" + "\n\n".join(blocks))
    return [{"role": "user", "content": "<conversation_history>\n"
             + ("\n\n".join(sections) or "(本时段没有聊天记录)") + "\n</conversation_history>"}]
