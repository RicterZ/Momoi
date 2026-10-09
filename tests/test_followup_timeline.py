from datetime import datetime, timezone
from xml.etree import ElementTree

import pytest

from momoi.storage import Store
from momoi.storage.core.migrations import MIGRATIONS, _add_turn_parent
from momoi.runtime.turn_support import pack_user_context


def test_old_database_upgrades_before_parent_index(tmp_path):
    path = tmp_path / "momoi.sqlite3"
    store = Store(path)
    store.begin_turn("owner", "owner", [])
    store._db.execute("DROP INDEX turns_parent")
    store._db.execute("ALTER TABLE turns DROP COLUMN parent_turn_id")
    store._db.execute(f"PRAGMA user_version={MIGRATIONS.index(_add_turn_parent)}")
    store._db.commit()
    store.close()
    store = Store(path)
    try:
        store.begin_turn("follow", "reply_followup", [], parent_turn_id="owner")
        assert store._db.execute("SELECT parent_turn_id FROM turns WHERE id='follow'").fetchone()[0] == "owner"
        with pytest.raises(ValueError, match="parent cannot change"):
            store.begin_turn("follow", "reply_followup", [], parent_turn_id="different")
        with pytest.raises(ValueError, match="own parent"):
            store.begin_turn("self", "owner", [], parent_turn_id="self")
    finally:
        store.close()


def test_followup_merges_across_months_without_merging_other_children(tmp_path):
    store = Store(tmp_path / "momoi.sqlite3")
    try:
        store.begin_turn("owner", "owner", [])
        store.begin_turn("follow", "reply_followup", [], parent_turn_id="owner")
        store.begin_turn("maintenance", "current_state_maintenance", [], parent_turn_id="owner")
        for tid, stage, month in [
            ("owner", "owner", 8), ("follow", "reply_followup", 9),
            ("maintenance", "current_state_maintenance", 9),
        ]:
            store.record_thinking_call(
                turn_id=tid, call_id=tid, stage=stage, reasoning=tid,
                created_at=datetime(2026, month, 15, tzinfo=timezone.utc).timestamp(),
            )
        page = store.dashboard_thinking(month="2026-09")
        items = {item["id"]: item for item in page["items"]}
        assert set(items) == {"owner", "maintenance"}
        assert items["owner"]["excerpt"] == "owner"
        assert items["owner"]["call_count"] == 2
        assert items["owner"]["turn_ids"] == ["owner", "follow"]
        detail = store.dashboard_thinking_detail("follow")
        assert detail["turn_id"] == "owner"
        assert [call["turn_id"] for call in detail["calls"]] == ["owner", "follow"]
        assert len(store.read_thinking("follow")["calls"]) == 1
    finally:
        store.close()


def test_followup_xml_is_not_wrapped_or_escaped():
    text = pack_user_context(("followup",
        '<followup parent_turn_id="owner" silent_minutes="6"><reason>A &amp; B</reason></followup>'))
    root = ElementTree.fromstring(text)
    assert root.attrib == {"parent_turn_id": "owner", "silent_minutes": "6"}
    assert root.find("reason").text == "A & B"
    assert root.find("followup") is None


@pytest.mark.parametrize("reasonings, expected", [
    (["", "Owner reasoning", ""], "Owner reasoning"),
    (["x" * 500, "follow"], "x" * 400),
    (["", ""], ""),
])
def test_merged_excerpt_uses_first_visible_reasoning(tmp_path, reasonings, expected):
    store = Store(tmp_path / "momoi.sqlite3")
    try:
        store.begin_turn("owner", "owner", [])
        store.begin_turn("follow", "reply_followup", [], parent_turn_id="owner")
        for index, reasoning in enumerate(reasonings):
            store.record_thinking_call(
                turn_id="owner" if index == 0 else "follow",
                call_id=str(index), stage="owner" if index == 0 else "reply_followup",
                created_at=100 + index, reasoning=reasoning,
            )
        item = store.dashboard_thinking(month="all")["items"][0]
        assert item["id"] == "owner"
        assert item["excerpt"] == expected
    finally:
        store.close()
