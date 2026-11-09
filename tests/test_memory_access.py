import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from momoi.models import AgentReply, IncomingMessage, ProviderResponse, ToolCall, TurnDraft
from momoi.runtime.agent import TurnExecutionSpec
from momoi.runtime.context.triggers import triggered_memory_context
from momoi.storage import Store
from momoi.tools.memory import MemoryTools
from tests.test_memory_catalog import seed
from tests.support import reply_call


def commit(store, draft, turn_id="access", workflow="owner"):
    store.begin_turn(turn_id, workflow, [])
    if workflow == "owner":
        store.commit_turn([], "", AgentReply([]), draft, turn_id=turn_id)
    else:
        store.commit_autonomous_turn("missing", draft, turn_id=turn_id)


def test_tools_count_hits_per_call_only_when_turn_commits(store):
    identifier = seed(store.memories, "coffee", ["food_drink"])
    tools, draft = MemoryTools(store), TurnDraft()
    call = ToolCall("memory_search", "memory_search", {"query": "无糖咖啡"})
    assert tools.execute(call, [], draft)["ok"]
    assert tools.execute(call, [], draft)["ok"]  # Same invocation is idempotent.
    assert store.memory_access_stats([identifier])[identifier]["total"] == 0
    commit(store, draft)
    item = store.list_memories()[0]["access"]
    assert item["counts"] == {"searched": 1, "browsed": 0, "read": 0, "recalled": 0, "always": 0}
    assert item["recent_30_days"] == item["total"] == 1
    assert item["last_at"] is not None
    draft = TurnDraft()
    tools.execute(ToolCall("second-search", "memory_search", {"query": "无糖咖啡"}), [], draft)
    commit(store, draft, "second")
    assert store.memory_access_stats([identifier])[identifier]["counts"]["searched"] == 2


def test_async_search_counts_once_and_failed_or_missing_results_do_not_count(store):
    identifier = seed(store.memories, "coffee", ["food_drink"])
    draft, tools = TurnDraft(), MemoryTools(store)
    result = asyncio.run(tools.execute_async(ToolCall("search", "memory_search", {"query": "无糖咖啡"}), [], draft))
    assert result["results"][0]["id"] == identifier
    tools.execute(ToolCall("empty", "memory_search", {"query": ""}), [], draft)
    tools.execute(ToolCall("miss", "memory_search", {"query": "不存在的词"}), [], draft)
    commit(store, draft)
    assert store.memory_access_stats([identifier])[identifier]["total"] == 1


def test_access_rolls_back_with_turn_and_can_retry(store):
    identifier = seed(store.memories, "coffee", [])
    draft = TurnDraft()
    draft.record_memory_access("read", [identifier])
    store.begin_turn("rollback", "owner", [])
    with patch.object(store, "_apply_goal_mutations", side_effect=RuntimeError("injected")):
        with pytest.raises(RuntimeError, match="injected"):
            store.commit_turn([], "", AgentReply([]), draft, turn_id="rollback")
    assert store.memory_access_stats([identifier])[identifier]["total"] == 0
    assert store._db.execute("SELECT state FROM turns WHERE id='rollback'").fetchone()[0] == "running"
    commit(store, draft, "rollback")
    assert store.memory_access_stats([identifier])[identifier]["total"] == 1


def test_replace_merge_and_scope_preserve_identity_history(store):
    first = seed(store.memories, "first", ["food_drink"])
    second = seed(store.memories, "second", ["health"])
    scoped = seed(store.memories, "first", [], scope="webhook")
    draft = TurnDraft()
    draft.record_memory_access("read", [first, second, scoped])
    commit(store, draft)
    replaced = store.update_memory_content(first, "新版")["id"]
    assert store.memory_access_stats([replaced])[replaced]["total"] == 1
    merged = store.memories.repository.merge(replaced, [second], "合并", "recall", None,
        [{"id": "merge", "content": "合并依据", "occurred_at": time.time()}])
    again = store.update_memory_content(merged, "再次更新")["id"]
    stats = store.memory_access_stats([again, scoped])
    assert stats[again]["total"] == 2
    assert stats[scoped]["total"] == 1


def test_retention_rolls_details_into_totals_without_losing_identity(store):
    identifier = seed(store.memories, "coffee", [])
    now = time.time()
    draft = TurnDraft()
    for index, days in enumerate([181, 180, 31, 30, 0]):
        draft.record_memory_access("read", [identifier], event_key=str(index))
        draft.memory_access[-1]["created_at"] = now - days * 86400
    commit(store, draft)
    store.compact_memory_access(now=now)
    store.compact_memory_access(now=now)  # Repeating maintenance cannot double totals.
    item = store.memory_access_stats([identifier], now=now)[identifier]
    assert item["total"] == 5 and item["recent_30_days"] == 2
    assert item["last_at"] == now
    assert store._db.execute("SELECT COUNT(*) FROM memory_access").fetchone()[0] <= 4
    replaced = store.update_memory_content(identifier, "新版")["id"]
    assert store.memory_access_stats([replaced], now=now)[replaced] == item


def test_always_prefix_generation_survives_rounds_turns_and_restart(daemon):
    identifier = seed(daemon.memory, "always", [], activation="always")
    async def request(turn_id):
        daemon.store.begin_turn(turn_id, "owner", [])
        messages = daemon.shared_turn_context(turn_id)["messages"]
        draft = TurnDraft()
        for _ in range(2):
            await daemon.model_round.run([], messages, [], complete=AsyncMock(return_value=ProviderResponse([], [])),
                system_policy=None, authority="owner", require_tool=False, required_tool=None,
                history_messages=0, stage="owner", turn_id=turn_id, round_number=1,
                channel="dashboard", goal_id=None, draft=draft)
        commit(daemon.store, draft, turn_id)
    asyncio.run(request("one"))
    asyncio.run(request("two"))
    assert daemon.store.memory_access_stats([identifier])[identifier]["counts"]["always"] == 1
    other = Store(daemon.config.database)
    try:
        generation = daemon.store.transcript_memory_context([])["access_generation"]
        assert other.transcript_memory_context([])["access_generation"] == generation
    finally:
        other.close()
    daemon.store.transcript_memory_context([], compact=True)
    asyncio.run(request("three"))
    assert daemon.store.memory_access_stats([identifier])[identifier]["counts"]["always"] == 2


def test_trigger_injection_is_counted_once_not_on_catalog_or_history_reads(store):
    identifier = seed(store.memories, "coffee", ["food_drink"])
    snapshot = store.memories.snapshots([identifier])[identifier]
    store.memories.repository.update_meta(snapshot, {"tags": ["food_drink"], "triggers": ["咖啡"]})
    event = IncomingMessage("e", "m", "咖啡", time.time(), time.time())
    draft = TurnDraft()
    assert triggered_memory_context(store.memories, [event], draft.memory_context, draft=draft)
    assert not triggered_memory_context(store.memories, [event], draft.memory_context, only_new=True, draft=draft)
    store.memory_catalog_context()
    store.transcript_memory_context([])
    commit(store, draft)
    assert store.memory_access_stats([identifier])[identifier]["counts"]["recalled"] == 1


def test_recall_tool_counts_only_each_observations_hits(daemon):
    first = seed(daemon.memory, "first", [])
    second = seed(daemon.memory, "second", [])
    calls = iter([
        ToolCall("recall-1", "recall", {"keyword": ["first"]}),
        ToolCall("recall-2", "recall", {"keyword": ["second"]}),
        reply_call("reply", bubbles=["已找到记录"]),
        ToolCall("end", "end_turn", {"mood": {"decision": "unchanged"}}),
    ])
    async def complete(*args, **kwargs):
        call = next(calls)
        return ProviderResponse([{"type": "tool_use", "id": call.id, "name": call.name,
                                  "input": call.arguments}], [call])
    draft = TurnDraft()
    daemon.provider = SimpleNamespace(complete=complete, config=SimpleNamespace(api_format="anthropic"))
    daemon.store.begin_turn("recall-test", "owner", [])
    observations = [{"memory_records": [{"id": first}]}, {"memory_records": [{"id": second}]}]
    with patch.object(daemon, "submit_owner_context", new=AsyncMock(side_effect=observations)):
        asyncio.run(daemon._run_tool_loop([], [{"role": "user", "content": "查记录"}],
            daemon.tool_surface.conversation_specs(), [], draft, execution=TurnExecutionSpec("owner"),
            source_event_id="synthetic", turn_id="recall-test", delivery_channel=daemon.channel))
    commit(daemon.store, draft, "recall-test")
    stats = daemon.store.memory_access_stats([first, second])
    assert stats[first]["counts"]["recalled"] == stats[second]["counts"]["recalled"] == 1


def test_summary_counts_calls_separately_from_hits_and_includes_unused(store):
    first = seed(store.memories, "first", [])
    seed(store.memories, "unused", [])
    store.begin_turn("summary", "owner", [])
    for call_id in ["search-1", "search-2"]:
        store.begin_tool_call("summary", call_id, "memory_search", {"query": "first"}, "read")
        store.complete_tool_call("summary", call_id, {"ok": True})
    draft = TurnDraft()
    draft.record_memory_access("searched", [first])
    commit(store, draft, "summary")
    summary = store.memory_access_summary()
    assert summary["memories"] == 2 and summary["unused_memories"] == 1
    assert summary["tool_calls"]["memory_search"] == {"total": 2, "recent_30_days": 2}
    assert summary["tool_calls"]["recall"]["total"] == 0
