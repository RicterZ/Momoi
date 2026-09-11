from copy import deepcopy
from xml.etree.ElementTree import fromstring

from momoi.runtime.workflows.memory_operation.rendering import render_memory_operation_request
from momoi.runtime.workflows.memory_maintenance.rendering import render_memory_maintenance_request
from momoi.runtime.workflows.memory_rendering import memory_record
from momoi.semantic.episode_cue_verifier import render_cue_review


def test_cue_xml_keeps_uncited_corrections_identity_and_multiple_quotes():
    claims = [
        {"message_id": 25, "turn_id": "persistent-later", "ordinal": 2, "role": "user",
         "delivery_state": "delivered", "quote": "其实没完成 <task> & 仍在处理"},
        {"message_id": 23, "turn_id": "persistent-earlier", "ordinal": 1, "role": "assistant",
         "delivery_state": "uncertain", "quote": '已经完成 "任务"'},
        {"message_id": 23, "turn_id": "persistent-earlier", "ordinal": 1, "role": "assistant",
         "delivery_state": "uncertain", "quote": "另一处引文"},
        {"message_id": 24, "turn_id": "persistent-earlier", "ordinal": 1, "role": "assistant",
         "delivery_state": "internal", "quote": "内部记录"},
    ]
    cues = [{"text": "检索任务结果 <xml>", "evidence_message_ids": [23]}]
    original = deepcopy(claims)
    rendered = render_cue_review(claims, cues)
    root = fromstring(rendered)
    messages = root.findall("sources/message")
    assert [node.get("id") for node in messages] == ["23", "24", "25"]
    assert [node.get("source") for node in messages] == ["ASSISTANT", "ASSISTANT", "OWNER"]
    assert [node.get("turn") for node in messages] == ["T-1", "T-1", "T-2"]
    assert messages[0].get("delivery") == "uncertain"
    assert messages[1].get("delivery") == "internal"
    assert messages[2].get("delivery") is None
    assert [node.text for node in messages[0]] == [claims[1]["quote"], claims[2]["quote"]]
    assert messages[2].findtext("quote") == claims[0]["quote"]
    assert root.find("cues/cue").attrib == {"index": "0", "sources": "23"}
    assert root.findtext("cues/cue") == cues[0]["text"]
    assert "persistent-" not in rendered
    assert claims == original


def example_memory(memory_id=1):
    return {"id": memory_id, "kind": "preference", "key": "reply.style", "activation": "recall",
            "content": "保留 <tag> & 原始\n空白", "updated_at": 123, "expires_at": None,
            "source_event_id": 'owner-"1', "evidence_quote": "保留 <tag>", "authority": "owner",
            "importance": 0.5, "created_at": 100, "superseded_by": None}


def test_memory_xml_distinguishes_stale_and_deleted_snapshots_without_internal_fields():
    old = example_memory()
    current = {**old, "content": "新的要求", "updated_at": 124}
    deleted = example_memory(2)
    evidence = [{"event_id": 'owner-"1', "content": "忘记旧要求 <operation />",
                 "occurred_at": "2026-09-11T12:00:00+08:00", "occurred_at_unix": 123}]
    operation = {"id": "op-1", "type": "replace", "target_id": 1, "event_id": 'owner-"1',
                 "content": "新的要求", "evidence": "忘记旧要求"}
    rendered = render_memory_operation_request(
        now=125, timestamp="2026-09-11T12:00:02+08:00",
        operations=[operation], visible={1: old, 2: deleted}, snapshots={1: current}, evidence=evidence,
    )
    root = fromstring("<request>" + rendered + "</request>")
    assert root.find("current_time").get("unix") == "125"
    assert root.find("operation_requests/operation").get("target_id") == "1"
    assert root.find("current_memories/memory").get("visible") == "true"
    assert root.findtext("current_memories/memory/content") == "新的要求"
    assert [node.get("id") for node in root.findall("outdated_visible_snapshots/memory")] == ["1", "2"]
    assert root.findtext("outdated_visible_snapshots/memory/content") == old["content"]
    assert root.findtext("owner_evidence/event") == evidence[0]["content"]
    assert root.find("owner_evidence/event").get("id") == 'owner-"1'
    projected = memory_record(old)
    for field in ("authority", "importance", "created_at", "superseded_by"):
        assert field not in projected
        assert field not in rendered
    assert old == example_memory()


def test_maintenance_directory_does_not_duplicate_supplied_memories():
    rows = [example_memory(i) for i in range(1, 4)]
    rendered = render_memory_maintenance_request(
        mutable_memories=rows[:1], context_memories=rows[1:2], memory_directory=rows, owner_evidence=[],
    )
    root = fromstring("<request>" + rendered + "</request>")
    assert [node.get("id") for node in root.findall(".//memory")] == ["1", "2", "3"]
    assert root.findtext("mutable_memories/memory/content") == rows[0]["content"]
    assert root.find("topic_context") is None
