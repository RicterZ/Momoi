import asyncio
import json
from pathlib import Path

import pytest

from momoi.channel.wechaty import WechatyConfig, create_channel
from momoi.models import IncomingMessage


def test_wechaty_config_uses_private_workspace_storage(tmp_path: Path) -> None:
    config = WechatyConfig.from_mapping({"owner_alias": " Momoi主人 "}, tmp_path)
    assert config.data_dir == tmp_path.resolve() / "channel" / "wechaty"
    assert config.owner_alias == "Momoi主人"


def test_wechaty_config_accepts_retired_owner_id(tmp_path: Path) -> None:
    assert WechatyConfig.from_mapping({"owner_id": "@abc"}, tmp_path).owner_alias == ""


@pytest.mark.parametrize("value", [None, {"unexpected": True}, {"owner_alias": "bad\0alias"}])
def test_wechaty_config_rejects_invalid_settings(tmp_path: Path, value) -> None:
    with pytest.raises(ValueError):
        WechatyConfig.from_mapping(value, tmp_path)


class _Stdin:
    def __init__(self):
        self.lines = []

    def write(self, data):
        self.lines.append(json.loads(data))

    async def drain(self):
        pass


class _Process:
    def __init__(self, events):
        self.stdout = asyncio.StreamReader()
        for event in events:
            self.stdout.feed_data((json.dumps(event) + "\n").encode())
        self.stdout.feed_eof()
        self.stdin = _Stdin()


def _message(sender, text, message_id):
    return {"type": "message", "id": message_id, "senderId": sender, "text": text}


def _received(channel, events):
    received = []

    async def on_event(event):
        received.append(event)

    async def run():
        channel._process = _Process(events)
        await channel._read_events(on_event)

    asyncio.run(run())
    return [event for event in received if isinstance(event, IncomingMessage)]


def test_worker_messages_are_forwarded_with_session_recipient(tmp_path: Path) -> None:
    channel = create_channel(WechatyConfig.from_mapping({"owner_alias": "Momoi主人"}, tmp_path))
    messages = _received(channel, [_message("@owner", "ping", "1"), _message("@owner", "  ", "2")])
    assert [message.text for message in messages] == ["ping"]
    assert messages[0].delivery_context == {"wechaty_recipient": "@owner"}
