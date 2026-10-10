import json

from momoi.models import IncomingMessage
from momoi.storage import Store


def test_dashboard_expands_old_owner_batch_without_changing_storage(tmp_path):
    store = Store(tmp_path / "db")
    try:
        for event_id, text, stamp in [("first", "先说一句", 10), ("update", "等等", 30)]:
            store.add_event(IncomingMessage(event_id, event_id, text, stamp, stamp))
        with store._db:
            owner = store._db.execute(
                "INSERT INTO messages(turn_id,role,content,created_at,source_event_ids_json) VALUES(?,?,?,?,?)",
                ("turn", "user", "先说一句\n等等", 10, json.dumps(["first", "update"])),
            ).lastrowid
            reply = store._db.execute(
                "INSERT INTO messages(turn_id,role,content,created_at,source_event_ids_json) VALUES(?,?,?,?,?)",
                ("turn", "assistant", "回应", 20, "[]"),
            ).lastrowid
        messages = [dict(row) for row in store._db.execute("SELECT * FROM messages ORDER BY id")]
        timeline = store.dashboard_message_timeline(messages)
        assert [row["content"] for row in timeline] == ["先说一句", "回应", "等等"]
        assert [row["created_at"] for row in timeline] == [10, 20, 30]
        assert len({row["id"] for row in timeline}) == 3
        assert store._db.execute("SELECT content FROM messages WHERE id=?", (owner,)).fetchone()[0] == "先说一句\n等等"
        with store._db:
            outbox = store._db.execute(
                "INSERT INTO outbox(turn_id,dedupe_key,text,state) VALUES(?,?,?,?)",
                ("turn", "reply", "回应", "superseded"),
            ).lastrowid
            store._db.execute("UPDATE messages SET outbox_id=?,delivery_state='failed' WHERE id=?", (outbox, reply))
        messages = [dict(row) for row in store._db.execute("SELECT * FROM messages ORDER BY id")]
        assert store.dashboard_message_timeline(messages)[1]["delivery_state"] == "cancelled"
        with store._db:
            store._db.execute("DELETE FROM events WHERE id='update'")
        assert store.dashboard_message_timeline(messages)[0]["content"] == "先说一句\n等等"
    finally:
        store.close()
