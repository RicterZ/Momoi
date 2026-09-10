import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from momoi.conversation_roles import speaker_label
from momoi.runtime.workflows.episode.rendering import _speaker
from momoi.storage.reflection_values import reflection_window
from momoi.storage import Store
from momoi.storage.episode_claims import render_verified_claims
from momoi.storage.migrations import SCHEMA_VERSION
from momoi.storage.semantic_documents import _message_parts
from momoi.tools.memory import _episode_match_excerpt


@pytest.mark.parametrize("role,label", [
    ("user", "OWNER"), ("assistant", "ASSISTANT"), ("event", "EVENT"),
    ("goal", "GOAL"), ("heartbeat", "HEARTBEAT"), ("", "UNKNOWN"),
])
def test_evidence_renderers_preserve_speaker_and_original_names(role, label):
    content = '优香提到桃井；MOMOI 是应用名\n"我" 保留原文'
    row = {"role": role, "delivery_state": "uncertain", "turn_id": "turn-1",
           "ordinal": 1, "content": content, "quote": content}
    assert speaker_label(role) == label
    assert _speaker(row) == (f"{label} delivery=uncertain" if role == "assistant" else label)
    parts = _message_parts(row)
    assert parts == [f"[{label} turn=turn-1 ordinal=1 delivery=uncertain] {content}"]
    excerpt = _episode_match_excerpt({"matches": [row]})
    assert excerpt == f"- [{label} ordinal=1] {json.dumps(content, ensure_ascii=False)}"
    summary = render_verified_claims([row])
    source = f"{label} delivery=uncertain" if role == "assistant" else label
    assert summary == f"- [source {source} turn=turn-1 ordinal=1] {json.dumps(content, ensure_ascii=False)}"


def test_reflection_preserves_delivery_and_owner_evidence(tmp_path):
    store = Store(tmp_path / "momoi.sqlite3", timezone="Asia/Shanghai")
    try:
        at = datetime(2026, 9, 8, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        rows = [("user", "delivered", "老师提到桃井"),
                ("assistant", "delivered", "我叫优香"),
                ("assistant", "internal", "MOMOI 内部原文"),
                ("assistant", "uncertain", "未确认投递"),
                ("event", "internal", "外部事件")]
        with store._db:
            for index, (role, delivery, content) in enumerate(rows):
                store._db.execute(
                    "INSERT INTO messages(turn_id,role,content,created_at,delivery_state,source_event_ids_json) VALUES (?,?,?,?,?,'[]')",
                    ("turn", role, content, at + index, delivery),
                )
        window = reflection_window("2026-09-08", "03:00", store.timezone)
        result = store.conversation_messages_for_turns(None, window=window)
        assert [(row["role"], row["delivery_state"], row["content"]) for row in result] == [
            row for row in rows if row[:2] != ("assistant", "internal")
        ]
    finally:
        store.close()


def test_speaker_migration_changes_metadata_not_names_or_quotes(tmp_path):
    path = tmp_path / "momoi.sqlite3"
    store = Store(path)
    episode_id = store.create_episode("老师和桃井讨论 MOMOI")["id"]
    claim = {"role": "assistant", "delivery_state": "internal", "turn_id": "turn",
             "ordinal": 1, "quote": "优香说桃井也在使用 MOMOI"}
    raw_claims = json.dumps([claim], ensure_ascii=False)
    with store._db:
        store._db.execute(
            "UPDATE conversation_episodes SET working_summary=?,working_summary_claims_json=?,"
            "emotional_context_json=?,narrative_summary=? WHERE id=?",
            ("old generated framing", raw_claims, '{"owner":"开心","momoi":"配合","tone":"轻松"}',
             "桃井是对话提到的人", episode_id),
        )
        store._db.execute(f"PRAGMA user_version={6}")
    store.close()
    migrated = Store(path)
    try:
        episode = migrated.episode(episode_id)
        assert episode["title"] == "老师和桃井讨论 MOMOI"
        assert episode["narrative_summary"] == "桃井是对话提到的人"
        assert episode["working_summary_claims"] == [claim]
        assert episode["working_summary"] == render_verified_claims([claim])
        assert episode["emotional_context"] == {"owner": "开心", "assistant": "配合", "tone": "轻松"}
    finally:
        migrated.close()
