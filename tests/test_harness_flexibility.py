from momoi.runtime.agent.protocol import MAX_CONSECUTIVE_THOUGHT_ROUNDS
from tests.support import reply_call, install_scripted_replyer
import asyncio
from types import SimpleNamespace

import pytest

from momoi.models import ProviderResponse, ToolCall
from momoi.runtime.agent.harness import TurnHarness
from momoi.runtime.agent.protocol import MAX_CONSECUTIVE_EXECUTION_FAILURES
from tests.test_memory_operations import daemon, event, response


def end_call():
    return ToolCall("end", "end_turn", {
        "mood": {"decision": "unchanged"},
    })


@pytest.mark.parametrize("internal_text", ["继续核对当前判断", "<bubble>这是思考里的示例</bubble>"])
def test_internal_thought_then_silent_completion_does_not_send(daemon, internal_text):
    source = event(daemon.store, text="先想一想，不用发消息")
    turn_id = daemon._turn_id(source.event_id)
    rounds = 0

    async def complete(system, messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        assert not kwargs["require_tool"]
        if rounds <= 3:
            return ProviderResponse([{"type": "text", "text": internal_text}], [])
        assert internal_text in str(messages)
        assert "熔断" not in str(messages)
        result = response(end_call())
        result.content.insert(0, {"type": "text", "text": "内部结论：不需要发送"})
        return result

    daemon.provider = SimpleNamespace(complete=complete)
    asyncio.run(daemon._complete_batch_turn([source], asyncio.Event(), turn_id))
    assert rounds == 1
    assert not daemon.store.due_outbox()
    assert daemon.store._db.execute("SELECT state FROM turns WHERE id=?", (turn_id,)).fetchone()[0] == "completed"


@pytest.mark.parametrize("failures", [4, MAX_CONSECUTIVE_EXECUTION_FAILURES])
def test_execution_failures_have_separate_budget_and_bounded_recovery(daemon, failures):
    install_scripted_replyer(daemon)
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
            calls = [reply_call("notice", bubbles=["查找没有成功，先停下。"]), end_call()]
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
    assert harness.validate([ToolCall("voice", "reply", {}), end_call()], has_assistant_text=True) is None
    assert harness.validate([ToolCall("read", "read_file", {}), end_call()]) == "end_turn_must_be_alone"
    assert harness.validate([end_call(), ToolCall("send", "reply", {})]) == "end_turn_must_be_alone"


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


@pytest.mark.parametrize("stage", ["owner", "heartbeat", "webhook", "goal", "plan_step"])
def test_hidden_delivery_tools_cannot_bypass_replyer(stage):
    harness = TurnHarness.for_stage(stage)
    if harness.spec.first_tool:
        harness.accept(harness.spec.first_tool)
    for name in ("send_bubbles", "send_voice"):
        assert harness.validate([ToolCall("hidden", name, {})]) == "tool_not_allowed"


@pytest.mark.parametrize("recovery", ["blocked_tool", "silent_end", "text_forever", "provider_error", "sent_then_error"])
def test_circuit_recovery_is_bounded_and_cannot_restart_task(daemon, recovery):
    install_scripted_replyer(daemon)
    source = event(daemon.store, text="帮我处理一下")
    turn_id = daemon._turn_id(source.event_id)
    limit = daemon.config.turn_max_protocol_retries
    rounds = 0
    notices = []

    async def complete(system, messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds <= limit:
            return response(ToolCall(str(rounds), "heartbeat_activity", {}))
        assert {"reply"} <= {t["name"] for t in tools}
        assert "熔断" in str(messages)
        assert "错误摘要" in str(messages)
        step = rounds - limit
        if recovery == "provider_error" or (recovery == "sent_then_error" and step > 1):
            raise RuntimeError("private upstream secret")
        if recovery == "text_forever":
            return ProviderResponse([{"type": "text", "text": "还是不用工具"}], [])
        if step == 1 and recovery == "blocked_tool":
            return response(ToolCall("forbidden", "write_file", {"path": "no", "content": "no"}))
        if step == 1 and recovery == "silent_end":
            return response(ToolCall("silent-end", "end_turn", {
                "mood": {"decision": "unchanged"},
            }))
        if recovery == "sent_then_error" or step == 2:
            notices.append("任务没做完，工具连续出错，我先停下了。")
            return response(reply_call("notice", bubbles=notices[-1:]))
        return response(ToolCall("finish", "end_turn", {
            "mood": {"decision": "unchanged"},
        }))

    daemon.provider = SimpleNamespace(complete=complete, config=SimpleNamespace(api_format="anthropic"))
    asyncio.run(daemon._complete_batch_turn([source], asyncio.Event(), turn_id))
    assert rounds <= limit + 3
    outbox = daemon.store.due_outbox()
    assert len(outbox) == 1
    assert "private upstream secret" not in outbox[0].text
    if notices:
        assert outbox[0].text == notices[0]
    else:
        assert outbox[0].text == "这次处理连续出错，已经停下了，没能完成。"
    assert not daemon.store._db.execute(
        "SELECT 1 FROM tool_audit WHERE turn_id=? AND tool_name='write_file'", (turn_id,)
    ).fetchone()
