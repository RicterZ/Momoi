import asyncio
import copy
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.models import (
    AgentReply,
    IncomingMessage,
    ProviderResponse,
    ToolCall,
    TurnDraft,
)
from momoi.runtime import MomoiDaemon
from momoi.runtime.agent.harness import TurnHarness
from momoi.runtime.workflows.memory_operation.parsing import parse_decisions
from momoi.storage import Store
from momoi.tools.memory import MemoryTools
from tests.support import provider_catalog, seed_memory


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "memory.sqlite3")
    yield value
    value.close()


def event(store, name="owner", text="以后喝茶，记住", timestamp=None):
    now = time.time() if timestamp is None else timestamp
    value = IncomingMessage(name, name, text, now, now)
    store.add_event(value)
    return value


def submit(
    store,
    source,
    *,
    target=None,
    action="add",
    name="op",
    turn_id="source",
    context=None,
):
    draft = TurnDraft(
        memory_context=context or {},
        memory_conversation=[{"role": "user", "content": "相关上文"}],
    )
    args = {"type": action, "content": source.text, "evidence": source.text}
    if target is not None:
        args["target_id"] = target
    result = MemoryTools(store).execute(
        ToolCall(name, "memory_operation", args), [source], draft
    )
    assert result["ok"], result
    store.commit_turn([source], source.text, AgentReply([]), draft, turn_id=turn_id)
    return draft


def write(source, *, ids=None, targets=None, key="drink", content="主人喝茶"):
    return {
        "operation_ids": ids or ["op"],
        "action": "write",
        "reason": "主人明确更新",
        "target_ids": targets or [],
        "memory": {
            "kind": "preference",
            "key": key,
            "content": content,
            "activation": "recall",
            "expires_at": None,
        },
        "evidence": [{"event_id": source.event_id, "quote": source.text}],
    }


def apply(store, batch, decisions, snapshots=None):
    snapshots = snapshots or {}
    evidence = {item["event_id"]: item["text"] for item in batch["events"]}
    for item in store.memory_maintenance_evidence_for_memories(list(snapshots)):
        evidence[item["event_id"]] = item["content"]
    decisions = parse_decisions(
        {"decisions": decisions}, batch["operations"], snapshots, evidence, 720
    )
    store.apply_memory_operation(batch, decisions, snapshots)


def test_frontend_queues_only_on_commit_and_deduplicates(store):
    source = event(store)
    draft = TurnDraft()
    tools = MemoryTools(store)
    call = ToolCall(
        "op", "memory_operation", {"type": "add", "content": "喝茶", "evidence": "喝茶"}
    )
    result = tools.execute(call, [source], draft)
    assert result["state"] == "accepted"
    assert tools.execute(call, [source], draft) == result
    assert len(draft.memory_operations) == 1
    assert store.pending_memory_operation() is None
    assert not store.has_memory("preference", "drink")
    changed = ToolCall("op", "memory_operation", {**call.arguments, "content": "喝水"})
    assert tools.execute(changed, [source], draft)["error"] == "tool_call_id_conflict"
    store.commit_turn([source], source.text, AgentReply([]), draft, turn_id="source")
    assert store.pending_memory_operation() == "source"
    assert not store.maintenance_memory_inventory()


@pytest.mark.parametrize(
    "args",
    [
        {"type": "forge"},
        {"type": []},
        {"target_id": 999},
        {"target_id": True},
        {"evidence": "不存在的原话"},
        {"content": " "},
        {"ttl": 12},
    ],
)
def test_frontend_rejects_invalid_requests(store, args):
    source = event(store)
    draft = TurnDraft()
    arguments = {"type": "add", "content": source.text, "evidence": source.text, **args}
    assert not MemoryTools(store).execute(
        ToolCall("op", "memory_operation", arguments), [source], draft
    )["ok"]
    assert not draft.memory_operations


def test_no_operation_means_no_review_and_failed_commit_rolls_back(store):
    source = event(store)
    store.commit_turn([source], source.text, AgentReply([]), turn_id="ordinary")
    assert store.pending_memory_operation() is None
    draft = TurnDraft(
        memory_operations=[
            {"id": "op", "event_id": source.event_id, "evidence": "wrong"}
        ]
    )
    with pytest.raises(ValueError, match="evidence_changed"):
        store.commit_turn(
            [source], source.text, AgentReply([]), draft, turn_id="failed"
        )
    assert store._db.execute("SELECT 1 FROM turns WHERE id='failed'").fetchone() is None
    assert store.pending_memory_operation() is None


def test_add_replace_merge_and_forget_are_atomic(store):
    old = event(store, "old", "以前喝咖啡", time.time() - 20)
    first = seed_memory(store, old, key="drink", content="咖啡")
    second = seed_memory(store, old, key="drink.duplicate", content="爱喝咖啡")
    source = event(store)
    snapshots = store.memory_snapshots([first, second])
    submit(store, source, target=first, action="replace", context=snapshots)
    batch = store.claim_memory_operation("source")
    apply(store, batch, [write(source, targets=[first, second])], snapshots)
    current = store.active_memory("preference", "drink")
    assert current["content"] == "主人喝茶"
    assert not store.memory_snapshots([first, second])
    assert len(store.maintenance_memory_inventory()) == 1
    assert (
        len(
            store._db.execute(
                "SELECT * FROM memory_evidence WHERE memory_id=?", (current["id"],)
            ).fetchall()
        )
        == 2
    )
    forgotten = event(store, "forget", "忘记饮品偏好", time.time() + 1)
    snapshots = store.memory_snapshots([current["id"]])
    submit(
        store,
        forgotten,
        target=current["id"],
        action="forget",
        context=snapshots,
        turn_id="forget",
    )
    batch = store.claim_memory_operation("forget")
    decision = {
        "operation_ids": ["op"],
        "action": "forget",
        "reason": "明确要求忘记",
        "target_ids": [current["id"]],
        "evidence": [{"event_id": forgotten.event_id, "quote": forgotten.text}],
    }
    apply(store, batch, [decision], snapshots)
    assert store.active_memory("preference", "drink") is None
    assert not store.search_memories("喝茶", 10)


@pytest.mark.parametrize("action", ["noop", "defer"])
def test_noop_defer_complete_without_writes(store, action):
    source = event(store)
    submit(store, source)
    batch = store.claim_memory_operation("source")
    apply(
        store,
        batch,
        [{"operation_ids": ["op"], "action": action, "reason": "已有事实或证据不明"}],
    )
    assert store.pending_memory_operation() is None
    assert (
        store._db.execute("SELECT state FROM memory_operation_batches").fetchone()[0]
        == "completed"
    )
    assert not store.maintenance_memory_inventory()


def test_grouped_requests_require_complete_coverage_and_evidence(store):
    source = event(store)
    draft = submit(store, source)
    operations = [
        *draft.memory_operations,
        {**draft.memory_operations[0], "id": "second"},
    ]
    decision = write(source, ids=["op", "second"])
    assert parse_decisions(
        {"decisions": [decision]}, operations, {}, {source.event_id: source.text}, 720
    )
    for invalid in (
        [write(source)],
        [decision, decision],
        [{**decision, "evidence": []}],
    ):
        with pytest.raises(ValueError):
            parse_decisions(
                {"decisions": invalid},
                operations,
                {},
                {source.event_id: source.text},
                720,
            )


def test_failure_rolls_back_whole_batch_then_accepts_correction(store):
    source = event(store)
    draft = TurnDraft()
    tools = MemoryTools(store)
    for name in ("op", "second"):
        tools.execute(
            ToolCall(
                name,
                "memory_operation",
                {"type": "add", "content": source.text, "evidence": source.text},
            ),
            [source],
            draft,
        )
    existing = seed_memory(store, source, key="conflict", content="既有事实")
    store.commit_turn([source], source.text, AgentReply([]), draft, turn_id="source")
    batch = store.claim_memory_operation("source")
    with pytest.raises(ValueError, match="memory_key_conflict"):
        apply(
            store, batch, [write(source), write(source, ids=["second"], key="conflict")]
        )
    assert store.active_memory("preference", "drink") is None
    assert store.memory_snapshots([existing])
    apply(store, batch, [write(source, ids=["op", "second"])])
    assert store.active_memory("preference", "drink")


def test_concurrent_memory_edit_prevents_commit(store):
    old = event(store, "old", "旧偏好", time.time() - 10)
    target = seed_memory(store, old, key="drink", content=old.text)
    source = event(store)
    snapshots = store.memory_snapshots([target])
    submit(store, source, target=target, action="replace", context=snapshots)
    batch = store.claim_memory_operation("source")
    with store._db:
        store._db.execute(
            "UPDATE memories SET content='后台更正' WHERE id=?", (target,)
        )
    with pytest.raises(ValueError, match="snapshot_changed"):
        store.apply_memory_operation(
            batch, [write(source, targets=[target])], snapshots
        )
    assert (
        store._db.execute("SELECT state FROM memory_operation_batches").fetchone()[0]
        == "running"
    )


def test_fifo_retry_blocks_later_requests(store):
    first = event(store, "first")
    submit(store, first, turn_id="first")
    second = event(store, "second")
    submit(store, second, turn_id="second")
    batch = store.claim_memory_operation("first")
    assert store.claim_memory_operation("second") is None
    store.release_memory_operation("first", batch["turn_id"], "timeout")
    assert store.pending_memory_operation() is None
    assert store.claim_memory_operation("second") is None
    with store._db:
        store._db.execute(
            "UPDATE memory_operation_batches SET retry_at=0 WHERE id='first'"
        )
    batch = store.claim_memory_operation("first")
    apply(store, batch, [write(first)])
    assert store.pending_memory_operation() == "second"


def test_readd_never_unhides_deleted_versions(store):
    old = event(store, "old", "喜欢喝茶")
    target = seed_memory(store, old, key="drink", content=old.text)
    with store._db:
        store._db.execute(
            "INSERT INTO memory_tombstones(kind,key,source_event_id,evidence_quote,created_at) VALUES (?,?,?,?,?)",
            ("preference", "drink", old.event_id, old.text, time.time()),
        )
    source = event(store, "readd", "重新记住喜欢喝茶")
    submit(store, source)
    batch = store.claim_memory_operation("source")
    apply(store, batch, [write(source)])
    assert not store.memory_snapshots([target])
    assert len(store.maintenance_memory_inventory()) == 1


def test_search_captures_authoritative_targets(store):
    source = event(store)
    target = seed_memory(store, source, key="drink", content=source.text)
    draft = TurnDraft()
    result = MemoryTools(store).execute(
        ToolCall("search", "memory_search", {"query": "喝茶"}), [source], draft
    )
    assert result["ok"]
    assert draft.memory_context[target]["content"] == source.text
    result = MemoryTools(store).execute(
        ToolCall(
            "op",
            "memory_operation",
            {
                "type": "replace",
                "content": source.text,
                "evidence": source.text,
                "target_id": target,
            },
        ),
        [source],
        draft,
    )
    assert result["ok"]


def test_recovery_and_retry_use_fresh_turns(store):
    source = event(store)
    submit(store, source)
    first = store.claim_memory_operation("source")
    assert store.claim_memory_operation("source") is None
    store.release_memory_operation("source", first["turn_id"], "provider timeout")
    assert store.pending_memory_operation() is None
    assert store.next_memory_operation_due_at() > time.time()
    with store._db:
        store._db.execute("UPDATE memory_operation_batches SET retry_at=0")
    second = store.claim_memory_operation("source")
    assert second["turn_id"] != first["turn_id"]
    store.recover_memory_operations()
    assert store.pending_memory_operation() == "source"
    assert (
        store._db.execute(
            "SELECT state FROM turns WHERE id=?", (second["turn_id"],)
        ).fetchone()[0]
        == "cancelled"
    )


@pytest.fixture
def daemon(tmp_path):
    value = MomoiDaemon(
        AppConfig(
            providers=provider_catalog(
                LLMConfig("http://127.0.0.1", "test", "test", 1000, 0, 1, 0)
            ),
            channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
            system_prompt="companion soul sentinel",
            database=tmp_path / "daemon.sqlite3",
            transcript_turns_min=4,
            transcript_turns_max=4,
            episode_raw_tail_turns=2,
            memory_results=6,
            log_level="INFO",
        )
    )
    yield value
    value.store.close()


def response(call):
    return ProviderResponse(
        [
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            }
        ],
        [call],
    )


def test_real_workflow_prompt_tool_correction_commit_and_usage(daemon):
    source = event(daemon.store)
    memory_id = seed_memory(
        daemon.store, source, key="other", content="上文事实", activation="always"
    )
    submit(daemon.store, source, context=daemon.store.memory_snapshots([memory_id]))
    calls = []

    async def complete(system, messages, tools=None, **kwargs):
        calls.append(copy.deepcopy(messages))
        assert "companion soul sentinel" not in str(system)
        assert {item["name"] for item in tools} == {
            "memory_operation_search",
            "memory_operation_finish",
        }
        request = json.loads(messages[0]["content"][0]["text"])
        assert request["current_memories"][0]["id"] == memory_id
        assert request["visible_memory_ids"] == [memory_id]
        assert not request["outdated_visible_snapshots"]
        assert request["conversation_context"][0]["content"] == "相关上文"
        assert request["owner_evidence"][0]["content"] == source.text
        decisions = [] if len(calls) == 1 else [write(source)]
        return response(
            ToolCall(
                f"finish-{len(calls)}",
                "memory_operation_finish",
                {"decisions": decisions},
            )
        )

    daemon.provider = type("Provider", (), {"complete": staticmethod(complete)})()
    asyncio.run(daemon._complete_memory_operation_turn("source", asyncio.Event()))
    assert len(calls) == 2
    assert "invalid_memory_operation_result" in json.dumps(calls[1])
    assert daemon.store.active_memory("preference", "drink")
    row = daemon.store._db.execute(
        "SELECT * FROM turns WHERE workflow_kind='memory_operation'"
    ).fetchone()
    assert row["state"] == "completed"
    assert row["llm_calls"] == 2
    assert daemon.store._db.execute("SELECT 1 FROM outbox").fetchone() is None


def test_optional_private_search_can_resolve_missing_target(daemon):
    source = event(daemon.store)
    target = seed_memory(
        daemon.store, source, key="drink", content="喝茶", activation="always"
    )
    submit(daemon.store, source, action="replace")
    count = 0
    daemon.semantic_recall.prepare = AsyncMock(return_value=None)

    async def complete(system, messages, tools=None, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return response(
                ToolCall("search", "memory_operation_search", {"query": "tea|喝茶"})
            )
        assert "喝茶" in json.dumps(messages, ensure_ascii=False)
        return response(
            ToolCall(
                "finish",
                "memory_operation_finish",
                {"decisions": [write(source, targets=[target])]},
            )
        )

    daemon.provider = type("Provider", (), {"complete": staticmethod(complete)})()
    asyncio.run(daemon._complete_memory_operation_turn("source", asyncio.Event()))
    assert count == 2
    assert not daemon.store.memory_snapshots([target])


@pytest.mark.parametrize("rejected_rounds", [1, 3])
def test_forbidden_tool_rejection_is_logged_before_retry_or_abort(
    daemon, caplog, rejected_rounds
):
    source = event(daemon.store)
    submit(daemon.store, source)
    count = 0

    async def complete(*args, **kwargs):
        nonlocal count
        count += 1
        assert not daemon.store.active_memory("preference", "drink")
        result = response(
            ToolCall(
                f"finish-{count}",
                "send_bubbles" if count <= rejected_rounds else "memory_operation_finish",
                {"decisions": [write(source)]},
            )
        )
        return result

    daemon.provider = type("Provider", (), {"complete": staticmethod(complete)})()
    with caplog.at_level("DEBUG", logger="momoi.runtime.turns"):
        asyncio.run(daemon._complete_memory_operation_turn("source", asyncio.Event()))

    rejections = [
        record.momoi_fields
        for record in caplog.records
        if getattr(record, "momoi_event", "") == "llm_protocol_rejected"
    ]
    assert len(rejections) == rejected_rounds
    for index, fields in enumerate(rejections, start=1):
        assert fields["reason"] == "tool_not_allowed"
        assert fields["stage"] == "memory_operation"
        assert fields["turn_id"] == "memory-operation:source:1"
        assert fields["call_id"]
        assert fields["round"] == index
        assert fields["consecutive_failures"] == index
        assert fields["failure_limit"] == 3
        assert fields["tool_names"] == ["send_bubbles"]
    batch = daemon.store._db.execute("SELECT * FROM memory_operation_batches").fetchone()
    if rejected_rounds == 1:
        assert count == 2
        assert batch["state"] == "completed"
        assert daemon.store.active_memory("preference", "drink")
    else:
        assert count == 3
        assert batch["state"] == "pending"
        assert batch["error"] == "tool_not_allowed"
        assert not daemon.store.active_memory("preference", "drink")


def test_new_owner_input_waits_for_running_review(daemon):
    source = event(daemon.store)
    submit(daemon.store, source)

    async def run():
        started = asyncio.Event()
        finish = asyncio.Event()

        async def complete(*args, **kwargs):
            started.set()
            await finish.wait()
            return response(
                ToolCall(
                    "finish", "memory_operation_finish", {"decisions": [write(source)]}
                )
            )

        daemon.provider = type("Provider", (), {"complete": staticmethod(complete)})()
        task = asyncio.create_task(
            daemon._complete_memory_operation_turn("source", asyncio.Event())
        )
        await asyncio.wait_for(started.wait(), 2)
        event(daemon.store, "new", "接下来聊别的")
        daemon._owner_message_changed.set()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(run())
    assert daemon.store.active_memory("preference", "drink")
    assert daemon.store.pending_events()[0].event_id == "new"


def test_explicit_cancellation_requeues_review(daemon):
    source = event(daemon.store)
    submit(daemon.store, source)

    async def run():
        started = asyncio.Event()

        async def complete(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        daemon.provider = type("Provider", (), {"complete": staticmethod(complete)})()
        task = asyncio.create_task(
            daemon._complete_memory_operation_turn("source", asyncio.Event())
        )
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert daemon.store.pending_memory_operation() == "source"
    assert not daemon.store.maintenance_memory_inventory()


def test_private_harness_rejects_messaging_and_combined_finish():
    harness = TurnHarness.for_stage("memory_operation")
    assert harness.validate([ToolCall("1", "send_bubbles", {})]) == "tool_not_allowed"
    assert harness.validate([ToolCall("1", "end_turn", {})]) == "tool_not_allowed"
    assert (
        harness.validate(
            [
                ToolCall("1", "memory_operation_finish", {}),
                ToolCall("2", "memory_operation_search", {}),
            ]
        )
        == "memory_operation_finish_must_be_alone"
    )


def test_existing_database_migration_preserves_foreign_keys_and_reopens(tmp_path):
    import momoi.storage

    path = tmp_path / "old.sqlite3"
    schema_path = Path(momoi.storage.__file__).with_name("schema.sql")
    old_schema = schema_path.read_text().replace("'memory_operation', ", "")
    old_schema = old_schema[
        : old_schema.index("CREATE TABLE IF NOT EXISTS memory_operation_batches")
    ]
    db = sqlite3.connect(path)
    db.executescript(old_schema)
    db.execute(
        "INSERT INTO turns(id,kind,workflow_kind,source_ids_json,state,started_at,updated_at) VALUES ('old','owner','owner','[]','completed',1,1)"
    )
    db.execute(
        "INSERT INTO messages(turn_id,role,content,created_at,source_event_ids_json) VALUES ('old','user','保留历史',1,'[]')"
    )
    db.execute("PRAGMA user_version=2")
    db.commit()
    db.close()
    for _ in range(2):
        upgraded = Store(path)
        assert (
            upgraded._db.execute(
                "SELECT content FROM messages WHERE turn_id='old'"
            ).fetchone()[0]
            == "保留历史"
        )
        assert not upgraded._db.execute("PRAGMA foreign_key_check").fetchall()
        assert upgraded._db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert upgraded._db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='turns_context_recent'"
        ).fetchone()
        assert (
            "'memory_operation'"
            in upgraded._db.execute(
                "SELECT sql FROM sqlite_master WHERE name='turns'"
            ).fetchone()[0]
        )
        upgraded.close()


@pytest.mark.parametrize("expiry", [None, True, float("inf"), -1, 10**15])
def test_recent_expiry_must_be_finite_future_and_bounded(store, expiry):
    source = event(store)
    draft = submit(store, source)
    decision = write(source)
    decision["memory"].update(activation="recent", expires_at=expiry)
    with pytest.raises(ValueError, match="recent expiry"):
        parse_decisions(
            {"decisions": [decision]},
            draft.memory_operations,
            {},
            {source.event_id: source.text},
            720,
        )


def test_forget_only_request_cannot_create_memory(store):
    source = event(store, text="忘记这个偏好")
    draft = submit(store, source, action="forget")
    with pytest.raises(ValueError, match="forget request cannot create"):
        parse_decisions(
            {"decisions": [write(source)]},
            draft.memory_operations,
            {},
            {source.event_id: source.text},
            720,
        )


def test_agent_worker_dispatches_memory_operation_without_extra_steps(daemon):
    source = event(daemon.store)
    submit(daemon.store, source)

    async def run():
        stop = asyncio.Event()

        async def complete(*args, **kwargs):
            stop.set()
            return response(
                ToolCall(
                    "finish", "memory_operation_finish", {"decisions": [write(source)]}
                )
            )

        daemon.provider = type("Provider", (), {"complete": staticmethod(complete)})()
        daemon._enqueue_memory_operation("source")
        daemon._enqueue_memory_operation("source")
        assert daemon.autonomous.qsize() == 1
        await asyncio.wait_for(daemon._agent_worker(stop), 2)

    asyncio.run(run())
    assert daemon.store.active_memory("preference", "drink")
    assert not daemon._queued_memory_operations


def test_owner_recall_snapshot_reaches_private_queue(daemon):
    from types import SimpleNamespace
    from tests.support import recall_response

    old = event(daemon.store, "old", "以前喝咖啡")
    target = seed_memory(daemon.store, old, key="drink", content="以前喝咖啡")
    daemon.store.commit_turn([old], old.text, AgentReply([]), turn_id="old")
    source = event(daemon.store)
    count = 0

    async def complete(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            initial = recall_response()
            initial.tool_calls[0].arguments["units"][0]["recall_queries"] = [
                {"semantic": "饮品偏好", "keywords": ["咖啡"]}
            ]
            return initial
        if count == 2:
            return response(
                ToolCall(
                    "op",
                    "memory_operation",
                    {
                        "type": "replace",
                        "content": source.text,
                        "evidence": source.text,
                        "target_id": target,
                    },
                )
            )
        return response(
            ToolCall(
                "end",
                "end_turn",
                {
                    "reply_wait": {"wait": False},
                    "mood": {"decision": "unchanged"},
                    "activity": {"decision": "unchanged"},
                },
            )
        )

    daemon.provider = SimpleNamespace(
        complete=complete, config=SimpleNamespace(api_format="anthropic")
    )
    asyncio.run(daemon._complete_batch_turn([source], asyncio.Event(), "source"))
    batch = daemon.store.claim_memory_operation("source")
    assert batch is not None
    assert batch["operations"][0]["target_id"] == target
    assert batch["context"][0]["id"] == target
    assert batch["context"][0]["content"] == old.text
    assert count == 3


def test_assistant_text_can_accompany_private_finish(daemon):
    source = event(daemon.store)
    submit(daemon.store, source)
    calls = 0

    async def complete(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = response(ToolCall('finish', 'memory_operation_finish', {'decisions': [write(source)]}))
        result.content.insert(0, {'type': 'text', 'text': 'I will apply this decision.'})
        return result

    daemon.provider = type('Provider', (), {'complete': staticmethod(complete)})()
    asyncio.run(daemon._complete_memory_operation_turn('source', asyncio.Event()))
    assert calls == 1
    assert daemon.store.active_memory('preference', 'drink')
    assert not daemon.store.due_outbox()


def test_owner_assistant_text_never_becomes_a_delivered_bubble(daemon):
    from types import SimpleNamespace
    from tests.support import recall_response

    source = event(daemon.store)
    replies = [
        recall_response(),
        response(ToolCall('send', 'send_bubbles', {'bubbles': ['这是气泡']})),
        response(ToolCall('end', 'end_turn', {
            'reply_wait': {'wait': False}, 'mood': {'decision': 'unchanged'},
            'activity': {'decision': 'unchanged'},
        })),
    ]
    for reply in replies:
        reply.content.insert(0, {'type': 'text', 'text': '正文不投递'})

    async def complete(*args, **kwargs):
        assert replies, 'Unexpected protocol retry'
        return replies.pop(0)

    daemon.provider = SimpleNamespace(complete=complete, config=SimpleNamespace(api_format='anthropic'))
    asyncio.run(daemon._complete_batch_turn([source], asyncio.Event(), 'source'))
    assert not replies
    assert [row.text for row in daemon.store.due_outbox()] == ['这是气泡']
    assert daemon.store._db.execute("SELECT state FROM turns WHERE id='source'").fetchone()[0] == 'completed'
