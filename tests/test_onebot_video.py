import asyncio
import base64
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from momoi.channel import AmbiguousSend, SendRejected
from momoi.channel.onebot import OneBotChannel, OneBotConfig
from momoi.storage import Store
from momoi.models import AgentReply


def channel():
    return OneBotChannel(OneBotConfig("ws://localhost", "20000", 1, 60, 30, 30, 20))


@pytest.mark.parametrize("mode", ["file", "video"])
def test_explicit_video_mode_and_separate_timeout(tmp_path, mode):
    async def scenario():
        item = channel()
        item.config = replace(item.config, video_send_timeout_seconds=95)
        item._request_action = AsyncMock(return_value={"data": {"message_id": "123"}})
        video = tmp_path / "camera.mp4"
        video.write_bytes(b"video")
        assert await item.send_message({"segments": [{
            "type": "video", "send_as": mode, "data": {"file": str(video)},
        }]}) == "123"
        request = item._request_action.await_args
        assert request.kwargs == {"timeout": 95}
        assert request.args[1]["message"] == [{"type": mode, "data": {
            "file": "base64://" + base64.b64encode(b"video").decode(),
            **({"name": "camera.mp4"} if mode == "file" else {}),
        }}]
        with pytest.raises(SendRejected):
            await item.send_message({"segments": [{"type": "video", "send_as": "other"}]})
    asyncio.run(scenario())


def test_video_timeout_is_ambiguous_without_fallback():
    async def scenario():
        received = []
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                received.append(frame.json())
            return ws
        app = web.Application()
        app.router.add_get("/", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        item = channel()
        item.config = replace(item.config, url=f"ws://127.0.0.1:{port}",
                              video_send_timeout_seconds=0.03)
        stop = asyncio.Event()
        task = asyncio.create_task(item.run(AsyncMock(), stop))
        try:
            await asyncio.wait_for(item._ready.wait(), 2)
            with pytest.raises(AmbiguousSend):
                await item.send_message({"segments": [{"type": "video", "send_as": "video",
                    "data": {"file": "base64://dmlkZW8="}}]})
            assert len(received) == 1
            assert received[0]["params"]["message"][0]["type"] == "video"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await runner.cleanup()
    asyncio.run(scenario())


def test_native_video_mode_survives_reply_storage(tmp_path):
    async def scenario():
        store = Store(tmp_path / "store.sqlite3", workspace=tmp_path)
        try:
            run, _ = store.create_webhook_run("notify", None, {"steps": [
                {"id": "notify", "uses": "message", "prompt": "event"},
            ]})
            store.start_webhook_step(run["id"], 0)
            store.commit_webhook_reply(run["id"], 0, "turn", AgentReply([
                {"segments": [{"type": "video", "send_as": "video",
                               "data": {"file": "base64://dmlkZW8="}}]},
            ]), "onebot")
            row, = store.due_outbox()
            item = channel()
            item._request_action = AsyncMock(return_value={"data": {"message_id": "123"}})
            await item.send_message(row.payload)
            assert item._request_action.await_args.args[1]["message"][0]["type"] == "video"
        finally:
            store.close()
    asyncio.run(scenario())


def test_ambiguous_webhook_video_is_terminal_and_does_not_block_channel(tmp_path):
    store = Store(tmp_path / "store.sqlite3", workspace=tmp_path)
    try:
        plan = {"steps": [{"id": "clip", "uses": "video", "send_as": "file"}]}
        run, _ = store.create_webhook_run("camera", None, plan)
        run_id = run["id"]
        store.begin_webhook_video(run_id, 0)
        store.record_webhook_video(run_id, 0, {"path": str(tmp_path / "clip.mp4")})
        outbox_id, = store.enqueue_webhook_video(run_id, 0, "onebot")
        assert store.mark_sending(outbox_id)
        store.mark_ambiguous(outbox_id, 1, "timeout")
        assert store.webhook_delivery_state(run_id, 0) == "ambiguous"
        assert not store.outbox_dispatchable(outbox_id)
        assert not store.mark_sending(outbox_id)
        assert store.due_outbox() == []
        with store._db:
            cursor = store._db.execute("""INSERT INTO outbox
                (turn_id,dedupe_key,text,target_channel) VALUES ('next','next','hello','onebot')""")
        assert [row.id for row in store.due_outbox()] == [cursor.lastrowid]
        store.close()
        store = Store(tmp_path / "store.sqlite3", workspace=tmp_path)
        assert not store.outbox_dispatchable(outbox_id)
        assert [row.id for row in store.due_outbox()] == [cursor.lastrowid]
    finally:
        store.close()
