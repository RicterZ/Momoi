import json
from dataclasses import asdict
from xml.etree import ElementTree

import pytest

from momoi.runtime.context.current_state import pack_current_turn_context
from momoi.runtime.workflows.current_state import resolve_state_evidence
from momoi.storage import Store
from momoi.storage.core.migrations import MIGRATIONS, _add_current_state_evidence
from momoi.storage.memory.current_state import CurrentStateManager, SlotInput


def arguments(**overrides):
    return {
        "add": [
            {
                "subject": "owner",
                "key": "meal.dinner",
                "value": "虾仁131克已出库",
                "ttl_seconds": 100,
                "status": "observed",
                "source_turn": "T-1",
                "source_id": "message:1",
                "source": "虾仁出库一半",
                "uncertainty": "是否吃完未知",
                **overrides,
            }
        ],
        "delete": [],
    }


def records(role="user"):
    return [
        {
            "source_id": "message:1",
            "turn_id": "original-turn",
            "role": role,
            "content": "小黄瓜出库两根，虾仁出库一半",
            "created_at": 800.0,
        }
    ]


def test_provenance_uses_evidence_time_not_maintenance_time_and_roundtrips(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    store.current_state = CurrentStateManager(store._db, clock=lambda: 1000)
    original = arguments()
    resolved = resolve_state_evidence(original, records(), {"original-turn": "T-1"})
    change = store.current_state.apply_arguments(
        resolved,
        source_turn_id="batch-last-turn",
        operation_id="one",
        expected_revision=0,
    )
    slot = change.added[0]
    assert slot.created_at == 1000 and slot.observed_at == 800
    assert slot.evidence_turn_id == "original-turn"
    assert original == arguments()  # Tool trace retains the exact model arguments.
    text = pack_current_turn_context(store, "heartbeat")
    node = ElementTree.fromstring(text).find("state")
    assert node.attrib == {
        "key": "owner.meal.dinner",
        "status": "observed",
        "observed_at": store.context_timestamp(800),
        "source_turn": "original-turn",
    }
    assert node.findtext("source") == "虾仁出库一半"
    assert node.find("source").get("role") == "user"
    assert node.findtext("uncertainty") == "是否吃完未知"
    private = ElementTree.fromstring(
        pack_current_turn_context(store, "owner", maintenance=True)
    ).find("state")
    assert private.get("id") == slot.id
    assert private.get("subject") == "owner"
    assert private.get("key") == "meal.dinner"
    store.close()
    store = Store(path)
    store.current_state = CurrentStateManager(store._db, clock=lambda: 1001)
    assert store.current_state.snapshot().slots == (slot,)
    store.current_state.apply(
        delete=[slot.id],
        source_turn_id="delete",
        operation_id="two",
        expected_revision=1,
    )
    restored = store.current_state.restore(1, operation_id="three", expected_revision=2)
    assert restored.added == (slot,)
    store.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_turn": "T-999"},
        {"source_id": "event:invented"},
        {"source_id": ""},
        {"source": "用户答应吃饭后告诉我"},
        {"source": ""},
    ],
)
def test_fabricated_or_missing_source_is_rejected(overrides):
    with pytest.raises(ValueError, match="source_quote"):
        resolve_state_evidence(
            arguments(**overrides), records(), {"original-turn": "T-1"}
        )


def test_assistant_suggestion_cannot_become_observed_owner_promise():
    with pytest.raises(ValueError, match="assistant_source"):
        resolve_state_evidence(
            arguments(), records("assistant"), {"original-turn": "T-1"}
        )
    resolved = resolve_state_evidence(
        arguments(status="inferred"), records("assistant"), {"original-turn": "T-1"}
    )
    assert resolved["add"][0]["source_role"] == "assistant"


def test_inferred_requires_uncertainty_and_future_time_rejected(tmp_path):
    store = Store(tmp_path / "state.db")
    manager = CurrentStateManager(store._db, clock=lambda: 1000)
    for overrides, error in [
        ({"uncertainty": ""}, "uncertainty"),
        ({"observed_at": 1001}, "observed_at"),
    ]:
        with pytest.raises(ValueError, match=error):
            manager.apply(
                add=[SlotInput("owner", "meal", "可能在做饭", 100, **overrides)],
                source_turn_id="turn",
                operation_id="op",
                expected_revision=0,
            )
    assert manager.snapshot().revision == 0
    store.close()


def test_old_database_and_history_upgrade_without_inventing_evidence(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    manager = CurrentStateManager(store._db, clock=lambda: 1000)
    change = manager.apply(
        add=[SlotInput("owner", "meal", "做饭", 100)],
        source_turn_id="old",
        operation_id="old",
        expected_revision=0,
    )
    # Simulate genuine pre-migration rows AND historical inverse changes.
    new_fields = (
        "status",
        "observed_at",
        "evidence_turn_id",
        "source_quote",
        "source_role",
        "uncertainty",
    )
    old_slot = {k: v for k, v in asdict(change.added[0]).items() if k not in new_fields}
    with store._db:
        for field in new_fields:
            store._db.execute(f"ALTER TABLE current_state_slots DROP COLUMN {field}")
        store._db.execute(
            "UPDATE current_state_changes SET added_json=?", (json.dumps([old_slot]),)
        )
        store._db.execute(
            f"PRAGMA user_version={MIGRATIONS.index(_add_current_state_evidence)}"
        )
    store.close()
    store = Store(path)
    manager = CurrentStateManager(store._db, clock=lambda: 1001)
    slot = manager.snapshot().slots[0]
    assert slot.value == "做饭"
    assert slot.observed_at == 0 and slot.evidence_turn_id == ""
    assert slot.status == "inferred" and "not verified" in slot.uncertainty
    assert manager.history()[0].added == (slot,)
    manager.apply(
        delete=[slot.id],
        source_turn_id="new",
        operation_id="delete",
        expected_revision=1,
    )
    assert manager.restore(1, operation_id="restore", expected_revision=2).added == (
        slot,
    )
    store.close()


def test_batched_owner_source_time_is_the_quoted_event_time(tmp_path):
    from momoi.models import IncomingMessage, AgentReply
    from momoi.runtime.workflows.current_state import state_evidence_rows

    store = Store(tmp_path / "state.db")
    events = [
        IncomingMessage("a", "a", "准备做饭", 700, 700),
        IncomingMessage("b", "b", "虾仁出库一半", 800, 800),
    ]
    for event in events:
        store.add_event(event)
    store.commit_turn(
        events,
        "\n".join(e.text for e in events),
        AgentReply([]),
        turn_id="original-turn",
    )
    rows = state_evidence_rows(
        store, store.conversation_messages_for_turns(["original-turn"])
    )
    resolved = resolve_state_evidence(arguments(source_id="event:b"), rows, {"original-turn": "T-1"})
    assert resolved["add"][0]["observed_at"] == 800
    store.close()


def test_identical_quotes_are_disambiguated_by_source_id():
    rows = records() + [{**records()[0], "source_id": "event:later", "created_at": 900}]
    resolved = resolve_state_evidence(
        arguments(source_id="event:later"), rows, {"original-turn": "T-1"}
    )
    assert resolved["add"][0]["observed_at"] == 900
    assert "source_id" not in resolved["add"][0]


def test_source_records_render_original_events_with_escaped_text(tmp_path):
    from momoi.models import IncomingMessage, AgentReply
    from momoi.runtime.workflows.current_state import state_evidence_rows, render_state_evidence

    store = Store(tmp_path / "state.db")
    try:
        events = [IncomingMessage("first", "1", "<在家> & 等你", 700, 700),
                  IncomingMessage("second", "2", "已经出门", 800, 800)]
        for event in events:
            store.add_event(event)
        store.commit_turn(events, "合并文本", AgentReply([]), turn_id="source")
        rows = state_evidence_rows(store, store.conversation_messages_for_turns(["source"]))
        rendered = render_state_evidence(store, rows, {"source": "T-1"})
        sources = ElementTree.fromstring("<evidence>" + rendered + "</evidence>")
        assert [item.get("id") for item in sources] == ["event:first", "event:second"]
        assert [item.text for item in sources] == [event.text for event in events]
        assert [item.get("time") for item in sources] == [store.context_timestamp(700), store.context_timestamp(800)]
    finally:
        store.close()
