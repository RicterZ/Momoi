import asyncio
from types import SimpleNamespace

import pytest

from momoi.models import ProviderResponse, ToolCall
from momoi.runtime.agent.harness import TurnHarness
from momoi.runtime.agent.protocol import MAX_CONSECUTIVE_EXECUTION_FAILURES
from tests.test_memory_operations import daemon, event, response


def end_call():
    return ToolCall("end", "end_turn", {
        "mood": {"decision": "unchanged"}, "reply_wait": {"wait": False},
    })


def test_internal_thought_then_silent_completion_does_not_send(daemon):
    source = event(daemon.store, text="先想一想，不用发消息")
    turn_id = daemon._turn_id(source.event_id)
    rounds = 0

    async def complete(system, messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        assert not kwargs["require_tool"]
        if rounds <= 3:
            return ProviderResponse([{"type": "text", "text": "继续核对当前判断"}], [])
        assert "继续核对当前判断" in str(messages)
        assert "熔断" not in str(messages)
        result = response(end_call())
        result.content.insert(0, {"type": "text", "text": "内部结论：不需要发送"})
        return result

    daemon.provider = SimpleNamespace(complete=complete)
    asyncio.run(daemon._complete_batch_turn([source], asyncio.Event(), turn_id))
    assert rounds == 4
    assert not daemon.store.due_outbox()
    assert daemon.store._db.execute("SELECT state FROM turns WHERE id=?", (turn_id,)).fetchone()[0] == "completed"


@pytest.mark.parametrize("failures", [4, MAX_CONSECUTIVE_EXECUTION_FAILURES])
def test_execution_failures_have_separate_budget_and_bounded_recovery(daemon, failures):
    source = event(daemon.store)
    turn_id = daemon._turn_id(source.event_id)
    rounds = 0
    executions = 0

    async def execute(call):
        nonlocal executions
        executions += 1
        return {"ok": False, "error": "file_not_found"}

    async def complete(system, messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds <= failures:
            assert "熔断" not in str(messages)
            return response(ToolCall(str(rounds), "read_file", {"path": f"missing-{rounds}"}))
        assert ("熔断" in str(messages)) == (failures == MAX_CONSECUTIVE_EXECUTION_FAILURES)
        if failures == MAX_CONSECUTIVE_EXECUTION_FAILURES:
            calls = [ToolCall("notice", "send_bubbles", {"bubbles": ["查找没有成功，先停下。"]}), end_call()]
            return ProviderResponse([
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                for c in calls
            ], calls)
        return response(end_call())

    daemon.builtin_tools.execute = execute
    daemon.provider = SimpleNamespace(complete=complete)
    asyncio.run(daemon._complete_batch_turn([source], asyncio.Event(), turn_id))
    assert executions == failures
    assert rounds == failures + 1


def test_end_allows_internal_text_but_keeps_read_result_boundary():
    harness = TurnHarness.for_stage("owner")
    assert harness.validate([end_call()], has_assistant_text=True) is None
    assert harness.validate([ToolCall("voice", "send_voice", {}), end_call()], has_assistant_text=True) is None
    assert harness.validate([ToolCall("read", "read_file", {}), end_call()]) == "end_turn_must_be_alone"
    assert harness.validate([end_call(), ToolCall("send", "send_bubbles", {})]) == "end_turn_must_be_alone"


def test_heartbeat_recall_covers_one_batch_not_future_sends():
    harness = TurnHarness.for_stage("heartbeat")
    harness.accept("heartbeat_begin")
    send = ToolCall("send", "send_bubbles", {})
    voice = ToolCall("voice", "send_voice", {})
    assert harness.validate([send, voice]) == "heartbeat_recall_required_before_send"
    harness.accept("recall")
    assert harness.validate([send, voice]) is None
    harness.accept("send_bubbles")
    harness.accept("send_voice")
    assert harness.validate([send]) == "heartbeat_recall_required_before_send"

    harness.accept("recall")
    harness.accept_owner_update()
    assert harness.validate([send]) == "heartbeat_recall_required_before_send"


def test_heartbeat_activity_and_end_commit_only_after_success(daemon):
    turn_id = "heartbeat-batched-finish"
    daemon.store.begin_turn(turn_id, "heartbeat", [f"heartbeat:{turn_id}"])
    rounds = 0

    async def complete(system, messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            return response(ToolCall("begin", "heartbeat_begin", {
                "activity": "rest", "mode": "rest", "strategy": [],
            }))
        assert rounds <= 3
        if rounds == 3:
            assert "end_turn_delivery_failed" in str(messages[-1])
        calls = [ToolCall(f"activity-{rounds}", "heartbeat_activity", {
            "activity": "rest", "result": "", "next_check_minutes": 0 if rounds == 2 else 30,
            "reason": "No new activity",
        }), end_call()]
        return ProviderResponse([
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
            for c in calls
        ], calls)

    daemon.provider = SimpleNamespace(complete=complete)
    asyncio.run(daemon._complete_heartbeat(turn_id, owner_event_revision=0))
    assert rounds == 3
    assert daemon.store._db.execute("SELECT state FROM turns WHERE id=?", (turn_id,)).fetchone()[0] == "completed"
