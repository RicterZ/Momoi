import base64
from zoneinfo import ZoneInfo

from momoi.models import IncomingMessage, AgentReply
from momoi.storage import Store
from momoi.channel.onebot.parsing import image_blocks
from momoi.runtime.turn_support import owner_content_blocks, tool_result_block
from momoi.runtime.transcript.building import build_transcript
from momoi.integrations.adapters.openai import openai_messages
from momoi.integrations.adapters.anthropic import merge_adjacent_roles

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


def incoming():
    return IncomingMessage(
        "pic",
        "pic",
        "看看这张图 [QQ image: source=embedded]",
        1,
        1,
        (
            {
                "type": "image",
                "data": {
                    "url": "base64://" + base64.b64encode(PNG).decode(),
                    "media_type": "image/png",
                },
            },
        ),
        channel="onebot",
    )


def test_image_survives_restart_and_history_is_text_only(tmp_path):
    path = tmp_path / "test.db"
    store = Store(path)
    event = incoming()
    assert store.add_event(event)
    identifier = event.segments[0]["data"]["_image_id"]
    blocks = owner_content_blocks([event], image_blocks, ZoneInfo("UTC"))
    assert any(identifier in b.get("text", "") for b in blocks)
    assert any(b["type"] == "image" for b in blocks)
    store.commit_turn([event], event.text, AgentReply([]), turn_id="turn-image")
    rows = store.conversation_messages_for_turns(["turn-image"])
    store._db.close()
    store = Store(path)
    original = store.read_image(identifier)
    assert base64.b64decode(original["source"]["data"]) == PNG
    transcript = build_transcript(
        store.image_history_rows(rows), timezone=ZoneInfo("UTC")
    )
    assert identifier in str(transcript.messages)
    assert all(b["type"] == "text" for m in transcript.messages for b in m["content"])
    assert identifier not in rows[0]["content"]
    assert store.read_image("../../etc/passwd") is None
    store._db.close()


def test_read_image_transport_is_multimodal_without_bytes_in_tool_json(tmp_path):
    store = Store(tmp_path / "test.db")
    event = incoming()
    store.add_event(event)
    identifier = event.segments[0]["data"]["_image_id"]
    result = tool_result_block("call", {"ok": True, "image_id": identifier})
    messages = [{"role": "user", "content": [result, store.read_image(identifier)]}]
    wire = openai_messages("", messages)
    assert wire[0]["role"] == "tool"
    assert "base64" not in wire[0]["content"]
    assert wire[1]["role"] == "user"
    assert wire[1]["content"][0]["type"] == "image_url"
    assert merge_adjacent_roles(messages)[0]["content"][1]["type"] == "image"
    store._db.close()


def test_summary_is_internal_and_survives_restart(tmp_path):
    path = tmp_path / "test.db"
    store = Store(path)
    event = incoming()
    store.add_event(event)
    identifier = event.segments[0]["data"]["_image_id"]
    assert store.missing_image_summaries([event]) == [identifier]
    assert not store.save_image_summary(identifier, " ")["ok"]
    assert not store.save_image_summary(identifier, "x" * 2001)["ok"]
    assert not store.save_image_summary("missing", "description")["ok"]
    assert store.save_image_summary(identifier, "红色外套，站在树旁；远处文字看不清。")[
        "ok"
    ]
    assert store.missing_image_summaries([event]) == []
    store.commit_turn([event], event.text, AgentReply([]), turn_id="image-summary")
    store.close()
    store = Store(path)
    rows = store.conversation_messages_for_turns(["image-summary"])
    assert "红色外套" not in str(rows)
    assert not store.due_outbox()
    enriched = store.image_history_rows(rows)
    assert "红色外套" in str(enriched)
    assert "base64" not in str(enriched)
    store.close()


def test_quoted_images_and_duplicates_are_durable(tmp_path):
    store = Store(tmp_path / "test.db")
    image = incoming().segments[0]
    segments = [{"type": "reply", "data": {"_quoted": {"segments": [image]}}}, image]
    with store._db:
        ids = store.register_images(segments)
    assert len(ids) == 2 and ids[0] == ids[1]
    assert store._db.execute("SELECT count(*) FROM visual_images").fetchone()[0] == 1
    store.close()


def test_local_image_is_copied_and_invalid_data_is_not_registered(tmp_path):
    store = Store(tmp_path / "test.db")
    original = tmp_path / "download.png"
    original.write_bytes(PNG)
    data = {"file": str(original), "media_type": "image/png"}
    with store._db:
        ids = store.register_images([{"type": "image", "data": data}], channel="weixin")
        invalid = store.register_images(
            [{"type": "image", "data": {"url": "base64://!!!"}}]
        )
    original.unlink()
    assert base64.b64decode(store.read_image(ids[0])["source"]["data"]) == PNG
    assert invalid == []
    store.close()


def test_legacy_event_gets_durable_reference_on_history_read(tmp_path):
    import json

    store = Store(tmp_path / "test.db")
    event = incoming()
    store.add_event(event)
    identifier = event.segments[0]["data"].pop("_image_id")
    with store._db:
        store._db.execute("DELETE FROM visual_images")
        store._db.execute(
            "UPDATE events SET payload_json=? WHERE id=?",
            (
                json.dumps({"channel": "onebot", "segments": event.segments}),
                event.event_id,
            ),
        )
    store.commit_turn([event], event.text, AgentReply([]), turn_id="legacy-image")
    rows = store.conversation_messages_for_turns(["legacy-image"])
    assert identifier in str(store.image_history_rows(rows))
    assert store.read_image(identifier) is not None
    stored = store._db.execute(
        "SELECT payload_json FROM events WHERE id=?", (event.event_id,)
    ).fetchone()[0]
    assert identifier in stored
    store.close()
