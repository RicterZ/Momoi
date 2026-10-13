import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from momoi.models import ProviderResponse, ToolCall, TurnDraft
from momoi.runtime import MomoiDaemon
from momoi.runtime.agent.harness import TurnHarness
from momoi.runtime.agent.tool_batch import ToolBatchRequest, ToolBatchState
from momoi.runtime.agent.workflow import TurnExecutionSpec
from tests.test_episode_annealing import config


@pytest.mark.parametrize("stage", ["owner", "webhook"])
def test_progress_reminder_counts_calls_and_resets_on_successful_reply(tmp_path, stage):
    daemon = MomoiDaemon(config(str(tmp_path)))
    daemon.store.begin_turn("progress", stage, [])
    messages = []
    state = ToolBatchState()
    harness = TurnHarness.for_stage(stage)
    serial = 0

    async def empty(*args):
        return []

    async def batch(names, *, ok=True, paused=False):
        nonlocal state, serial
        serial += 1
        calls = [ToolCall(f"{serial}-{index}", name,
                          {"url": "https://example.com"} if name == "web_fetch" else
                          {"mood": {"decision": "unchanged"}} if name == "end_turn" else {})
                 for index, name in enumerate(names)]
        response = ProviderResponse([
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            for call in calls
        ], calls)
        request = ToolBatchRequest(
            response=response, messages=messages,
            request_tools=[{"name": name} for name in {"web_fetch", "reply", "end_turn"}],
            tools=[], enable_tool_groups={}, current_events=[], draft=TurnDraft(),
            harness=harness, execution=TurnExecutionSpec(stage), source_event_id="",
            turn_id="progress", call_id=f"round-{serial}", round_number=serial,
            delivery_channel=daemon.channel, heartbeat_owner_event_revision=None,
            workflow=None, state=state, prepare_heartbeat_context=empty,
            submit_owner_context=empty, settle_owner_updates=empty,
            pause_requested=lambda: paused,
        )
        reply_result = {"ok": ok, "bubbles": ["Working on it"], "channel": daemon.channel.name}
        with patch.object(daemon.tool_batch.tool_executor.builtin_tools, "execute",
                          new=AsyncMock(return_value={"ok": ok, "error": None if ok else "read_failed"})), \
                patch.object(daemon.store, "committed_reply", return_value=reply_result), \
                patch.object(daemon.tool_batch.bubble_delivery, "wait_reply",
                             new=AsyncMock(return_value=reply_result)):
            result = await daemon.tool_batch.execute(request)
        state = result.state
        content = messages[-1]["content"]
        notices = [block for block in content if block.get("type") == "text"
                   and "reply_progress_reminder" in block.get("text", "")]
        assert len(result.results) == len(calls)
        assert all(block["type"] == "tool_result" for block in result.results)
        if notices:
            assert content[-1] == notices[0]
            assert "不要编造结果" in notices[0]["text"]
        return notices

    async def run():
        assert not await batch(["web_fetch", "web_fetch"])
        assert not await batch(["web_fetch"], paused=True)
        assert state.calls_since_reply == (2 if stage == "owner" else 0)
        notices = await batch(["web_fetch"], ok=False)
        assert bool(notices) == (stage == "owner")
        assert not await batch(["web_fetch", "web_fetch"])
        assert bool(await batch(["web_fetch"])) == (stage == "owner")
        # Crossing several thresholds in a parallel batch still produces one notice.
        notices = await batch(["web_fetch"] * 7)
        assert len(notices) == (1 if stage == "owner" else 0)
        assert not await batch(["reply"])
        assert state.calls_since_reply == 0
        assert not await batch(["web_fetch", "web_fetch"])
        assert not await batch(["reply"], ok=False)
        assert bool(await batch(["web_fetch"])) == (stage == "owner")
        assert not await batch(["web_fetch"] * 3 + ["reply"])
        assert state.calls_since_reply == 0
        rows = daemon.store._db.execute(
            "SELECT payload_json FROM turn_journal WHERE turn_id=? AND item_type='runtime_notice'",
            ("progress",),
        ).fetchall()
        assert len(rows) == (4 if stage == "owner" else 0)
        assert all(json.loads(row[0])["type"] == "reply_progress_reminder" for row in rows)
        assert not await batch(["web_fetch", "web_fetch"])
        assert not await batch(["end_turn"])
        assert state.calls_since_reply == (2 if stage == "owner" else 0)

    try:
        asyncio.run(run())
    finally:
        daemon.store.close()
