import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from momoi.models import IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime import MomoiDaemon
from momoi.runtime.jobs import AutonomousJob
from tests.support import with_owner_recall
from tests.test_episode_annealing import config


def test_multiple_natural_endings_continue_one_turn(tmp_path):
    daemon = MomoiDaemon(replace(config(str(tmp_path)), owner_continuation_seconds=.2))
    rounds = []
    async def complete(system, messages, tools, **kwargs):
        rounds.append(json.dumps(messages, ensure_ascii=False))
        call = ToolCall(f"end-{len(rounds)}", "end_turn", {"mood": {"decision": "unchanged"}})
        return ProviderResponse([{"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}], [call])
    async def run():
        daemon.provider = with_owner_recall(SimpleNamespace(complete=complete))
        first = IncomingMessage("first", "first", "第一句", 1, 1, channel=daemon.channel.name)
        daemon.store.add_event(first)
        task = asyncio.create_task(daemon._complete_batch_turn([first], asyncio.Event(), "continued"))
        daemon._active_turn = task
        try:
            for index, text in enumerate(["等等", "不对啊"], 1):
                while len(rounds) < index or not daemon._owner_continuation_waiting:
                    await asyncio.sleep(.001)
                event = IncomingMessage(str(index), str(index), text, index+1, index+1, channel=daemon.channel.name)
                daemon.store.add_event(event)
                daemon.incoming.put_nowait(event)
                while len(rounds) == index:
                    await asyncio.sleep(.001)
            await asyncio.wait_for(task, 2)
            assert len(rounds) == 3
            assert "等等" in rounds[1] and "不对啊" in rounds[2]
            assert "[用户中途插话]" in rounds[2]
            row = daemon.store._db.execute("SELECT state,source_ids_json FROM turns WHERE id='continued'").fetchone()
            assert row["state"] == "completed"
            assert json.loads(row["source_ids_json"]) == ["first", "1", "2"]
            assert not daemon._owner_continuation_waiting
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    try:
        asyncio.run(asyncio.wait_for(run(), 4))
    finally:
        daemon.store.close()


@pytest.mark.parametrize("interrupt", ["job", "webhook", "maintenance", "command", "other_channel", "cancel", "disabled"])
def test_continuation_yields_without_consuming_work(tmp_path, interrupt):
    daemon = MomoiDaemon(replace(config(str(tmp_path)), owner_continuation_seconds=0 if interrupt == "disabled" else 5))
    async def run():
        first = IncomingMessage("first", "first", "原消息", 1, 1, channel=daemon.channel.name)
        daemon._active_turn = asyncio.current_task()
        task = asyncio.create_task(daemon._wait_owner_continuation([first], daemon.channel))
        await asyncio.sleep(0)
        if interrupt == "job":
            daemon.autonomous.put_nowait(AutonomousJob.heartbeat())
        elif interrupt == "webhook":
            daemon.webhook_requests.put_nowait(("test", "turn", None))
        elif interrupt == "maintenance":
            daemon._owner_continuation_preempted = True
        elif interrupt in {"command", "other_channel"}:
            daemon.incoming.put_nowait(IncomingMessage("new", "new", "/stop" if interrupt == "command" else "你好",
                2, 2, channel=daemon.channel.name if interrupt == "command" else "another"))
        elif interrupt == "cancel":
            task.cancel()
        if interrupt == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await asyncio.wait_for(task, .5) == []
        assert not daemon._owner_continuation_waiting
        if interrupt == "job":
            assert not daemon.autonomous.empty()
        if interrupt == "webhook":
            assert not daemon.webhook_requests.empty()
        if interrupt in {"command", "other_channel"}:
            assert len(daemon._deferred_incoming) + daemon.incoming.qsize() == 1
    try:
        asyncio.run(run())
    finally:
        daemon.store.close()


def test_maintenance_preempts_window_before_starting(tmp_path):
    from unittest.mock import patch
    daemon = MomoiDaemon(config(str(tmp_path)))
    try:
        daemon._owner_continuation_waiting = True
        daemon._episode_annealing_dirty = True
        with patch.object(daemon, "_episode_annealing_ready", return_value=True):
            daemon._maybe_start_episode_annealing()
        assert daemon._owner_continuation_preempted
        assert daemon._active_annealing is None
        assert daemon._episode_annealing_dirty
    finally:
        daemon.store.close()
