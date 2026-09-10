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
    assert bodies[0].endswith('<bubble turn="T-1">\nold evidence\n</bubble>')
    runtime = ElementTree.fromstring("<record>" + bodies[1] + "</record>")
    assert runtime.find("turn").attrib == {"id": "T-2"}
    assert runtime.find("heartbeat").text.strip() == "activity result"
    assert bodies[2].endswith('<bubble turn="T-3">\nnew evidence\n</bubble>')
    assert ElementTree.fromstring(messages[-1]["content"]).attrib == {"id": "T-4", "evidence": "none"}
