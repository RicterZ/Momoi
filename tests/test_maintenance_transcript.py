from types import SimpleNamespace
from zoneinfo import ZoneInfo
from xml.etree import ElementTree

from momoi.runtime.transcript.maintenance import maintenance_transcript


def test_maintenance_references_follow_transcript_order_and_cover_runtime_records():
    store = SimpleNamespace(timezone=ZoneInfo("UTC"), turn_activity=lambda _: {})
    rows = [
        {"id": 3, "turn_id": "new", "role": "user", "content": "new evidence", "created_at": 3},
        {"id": 1, "turn_id": "old", "role": "user", "content": "old evidence", "created_at": 1},
        {"id": 2, "turn_id": "heartbeat", "role": "heartbeat", "content": "activity result", "created_at": 2},
    ]
    messages, labels = maintenance_transcript(store, rows, ["new", "old", "heartbeat", "silent"])
    assert labels == {"old": "T-1", "heartbeat": "T-2", "new": "T-3", "silent": "T-4"}
    bodies = [message["content"][0]["text"] for message in messages[:-1]]
    assert bodies[0] == '[message][1970-01-01T00:00:01+00:00][user][turn:T-1] old evidence'
    runtime = ElementTree.fromstring("<record>" + bodies[1] + "</record>")
    assert runtime.find("turn").attrib == {"id": "T-2"}
    assert runtime.find("heartbeat").text.strip() == "activity result"
    assert bodies[2] == '[message][1970-01-01T00:00:03+00:00][user][turn:T-3] new evidence'
    assert ElementTree.fromstring(messages[-1]["content"]).attrib == {"id": "T-4", "evidence": "none"}


def test_archival_native_replay_keeps_source_labels_without_legacy_recall():
    store = SimpleNamespace(
        timezone=ZoneInfo("UTC"),
        turn_activity=lambda _: (_ for _ in ()).throw(AssertionError("legacy activity queried")),
        turn_exchanges=lambda _: {"new": [{
            "content": [{"type": "tool_use", "id": "send", "name": "send_bubbles",
                         "input": {"bubbles": ["new reply"]}}],
            "results": [{"type": "tool_result", "tool_use_id": "send", "content": '{"ok":true}'}],
        }]},
    )
    rows = [
        {"id": 1, "turn_id": "old", "role": "user", "content": "old evidence", "created_at": 1},
        {"id": 2, "turn_id": "new", "role": "user", "content": "new evidence", "created_at": 2},
        {"id": 3, "turn_id": "new", "role": "assistant", "content": "new reply", "created_at": 3},
    ]
    messages, labels = maintenance_transcript(
        store, rows, ["old", "new"], include_activity=False, replay_native=True,
    )
    assert labels == {"old": "T-1", "new": "T-2"}
    assert "old evidence" in str(messages)
    assert "historical_recall" not in str(messages)
    assert str(messages).count("new reply") == 1
    assert any(block.get("type") == "tool_use" for message in messages
               for block in message["content"] if isinstance(block, dict))
