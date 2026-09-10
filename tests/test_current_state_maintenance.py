import asyncio
import copy
import json
import sqlite3
import time
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree

import pytest

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig, WebhookConfig
from momoi.integrations.models import LLMConfig
from momoi.models import (
    AgentReply,
    IncomingMessage,
    ProviderResponse,
    ToolCall,
    TurnDraft,
)
from momoi.runtime import MomoiDaemon
from momoi.runtime.agent import TurnExecutionSpec
from momoi.runtime.agent.harness import TURN_HARNESS_SPECS, TurnHarness
from momoi.runtime.jobs import AutonomousJob
from momoi.runtime.tool_contracts.current_state import current_state_finish_spec
from momoi.storage import Store
from momoi.storage.current_state import SlotInput
from momoi.storage.current_state_contract import CURRENT_STATE_SOURCE_STAGES
from momoi.storage.current_state_tasks import (
    CURRENT_STATE_BATCH_SIZE,
    CURRENT_STATE_FULL_IDLE_SECONDS,
    CURRENT_STATE_PARTIAL_IDLE_SECONDS,
)
from momoi.storage.migrations import MIGRATIONS, _add_current_state_workflow
from momoi.webhooks.catalog import bind_workflow
from momoi.webhooks.service import WebhookService
from tests.support import provider_catalog


@pytest.fixture
def daemon(tmp_path):
    value = MomoiDaemon(
        AppConfig(
            providers=provider_catalog(
                LLMConfig("http://127.0.0.1", "test", "test", 1000, 0, 1, 0)
            ),
            channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
            system_prompt="test",
            database=tmp_path / "test.sqlite3",
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


def finish(arguments=None):
    return response(
        ToolCall(
            "state-finish",
            "current_state_finish",
            arguments if arguments is not None else {"add": [], "delete": []},
        )
    )


def task_row(store, source="source"):
    row = store._db.execute(
        "SELECT * FROM current_state_tasks WHERE source_turn_id=?", (source,)
    ).fetchone()
    return dict(row) if row else None


def pending_state_source(store):
    status = store.current_state_batch_status()
    return str(status["source_turn_id"]) if status else None


def stage(store, source="source", kind="owner", *, commit=True):
    store.begin_turn(source, kind, [])
    messages = [
        {"role": "user", "content": "HISTORICAL"},
        {"role": "user", "content": "CURRENT_INPUT"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "work",
                    "name": "curl",
                    "input": {"url": "https://example.com"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "work",
                    "content": "TOOL_EVIDENCE",
                }
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "end", "name": "end_turn", "input": {}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "end", "content": '{"ok":true}'}
            ],
        },
    ]
    system = [{"type": "text", "text": "ORIGINAL_SYSTEM"}]
    tools = [
        {
            "name": "mcp__test__lookup",
            "description": "Enabled MCP tool",
            "input_schema": {"type": "object"},
        },
        current_state_finish_spec(),
        {
            "name": "end_turn",
            "description": "Original stage contract",
            "input_schema": {"type": "object"},
        },
    ]
    assert store.stage_current_state_task(source, kind, system, tools)
    if commit:
        store.complete_background_turn(source)
    return system, messages


@pytest.mark.parametrize("kind", sorted(CURRENT_STATE_SOURCE_STAGES))
def test_allowed_end_turn_captures_tool_surface_and_waits_for_commit(daemon, kind):
    source = "source"
    daemon.store.begin_turn(source, kind, [])
    calls = []
    if kind == "owner":
        calls.append(
            response(
                ToolCall(
                    "recall",
                    "recall",
                    {
                        "units": [
                            {
                                "intent": "current input",
                                "recall_mode": "skip",
                                "recall_queries": [],
                                "recall_from_turn_id": "",
                                "episode": {"action": "none"},
                            }
                        ]
                    },
                )
            )
        )
    if kind == "heartbeat":
        calls.extend(
            [
                response(
                    ToolCall(
                        "begin",
                        "heartbeat_begin",
                        {
                            "activity": "rest",
                            "mode": "rest",
                            "recall_mode": "skip",
                            "recall_queries": [],
                            "tool_groups": [],
                            "strategy": [],
                        },
                    )
                ),
                response(
                    ToolCall(
                        "activity",
                        "heartbeat_activity",
                        {"activity": "rest", "result": "", "next_check_minutes": 30, "reason": "rest"},
                    )
                ),
            ]
        )
    if kind == "reply_followup":
        daemon.store.pending_owner_reply = lambda: {"source_turn": "previous"}
        calls.append(
            response(ToolCall("send", "send_bubbles", {"bubbles": ["followup"]}))
        )
    end = {"mood": {"decision": "unchanged"}, "reply_wait": {"wait": False}}
    if kind == "goal":
        end = {}
        calls.append(response(ToolCall("review", "goal_review", {"status": "done", "result": "done"})))
        daemon.agenda_tools.finish_review = lambda *args: {"ok": True}
    calls.append(response(ToolCall("end", "end_turn", end)))
    systems = []
    surfaces = []
    request_chains = []

    async def complete(system, messages, tools, **kwargs):
        systems.append(copy.deepcopy(system))
        surfaces.append(copy.deepcopy(tools))
        request_chains.append(copy.deepcopy(messages))
        assert calls, str(messages[-1])
        return calls.pop(0)

    daemon.provider = SimpleNamespace(
        complete=complete, config=SimpleNamespace(api_format="anthropic")
    )
    messages = [
        {"role": "user", "content": "history"},
        {"role": "user", "content": "current"},
    ]
    asyncio.run(
        daemon._run_tool_loop(
            [{"type": "text", "text": "system"}],
            messages,
            daemon.tool_surface.conversation_specs(),
            [IncomingMessage("event", "event", "current", time.time(), time.time())]
            if kind == "owner"
            else [],
            TurnDraft(),
            execution=TurnExecutionSpec(
                kind,
                goal_id="goal" if kind == "goal" else None,
                permitted_tools=daemon.tool_surface.permitted_names(kind),
            ),
            source_event_id="event",
            turn_id=source,
            delivery_channel=daemon.channel,
        )
    )
    row = task_row(daemon.store)
    assert row["state"] == "staged"
    payload = json.loads(row["payload_json"])
    assert set(payload) == {"system", "tools"}
    assert payload["system"] == systems[-1]
    assert payload["tools"] == surfaces[-1]
    assert "current_state_finish" in {tool["name"] for tool in surfaces[0]}
    assert pending_state_source(daemon.store) is None
    daemon.store.complete_background_turn(source)
    assert pending_state_source(daemon.store) == source
    assert task_row(daemon.store)["state"] == "pending"

    async def maintain(system, messages, tools, **kwargs):
        assert tools == surfaces[-1]
        assert system == systems[-1]
        assert ElementTree.fromstring(messages[0]["content"]).attrib == {
            "id": "T-1", "evidence": "none",
        }
        return finish()

    daemon.provider.complete = maintain
    asyncio.run(daemon._complete_current_state_task(source))
    assert task_row(daemon.store)["state"] == "completed"


@pytest.mark.parametrize(
    "kind", sorted(set(TURN_HARNESS_SPECS) - CURRENT_STATE_SOURCE_STAGES)
)
def test_other_stages_never_schedule_state_maintenance(daemon, kind):
    with pytest.raises(ValueError, match="source_not_allowed"):
        daemon.store.stage_current_state_task("none", kind, [], [])
    assert task_row(daemon.store) is None


def test_commit_rollback_cancel_and_delete_never_leave_runnable_task(daemon):
    stage(daemon.store, commit=False)
    with pytest.raises(RuntimeError):
        with daemon.store._db:
            daemon.store._db.execute(
                "UPDATE turns SET state='completed' WHERE id='source'"
            )
            raise RuntimeError("commit failed")
    assert task_row(daemon.store)["state"] == "staged"
    assert pending_state_source(daemon.store) is None
    daemon.store.cancel_turn("source")
    assert task_row(daemon.store) is None
    stage(daemon.store, "delete-me")
    with daemon.store._db:
        daemon.store._db.execute("DELETE FROM turns WHERE id='delete-me'")
    assert task_row(daemon.store, "delete-me") is None


def test_maintenance_preserves_tools_without_replaying_source_chain(daemon):
    original_system, _ = stage(daemon.store)
    original_tools = json.loads(task_row(daemon.store)["payload_json"])["tools"]
    before = [tuple(row) for row in daemon.store._db.execute("SELECT * FROM messages")]
    requests = []

    async def complete(system, messages, tools, **kwargs):
        requests.append(copy.deepcopy(messages))
        assert system == original_system
        assert len(messages) == 2
        assert ElementTree.fromstring(messages[0]["content"]).attrib == {
            "id": "T-1", "evidence": "none",
        }
        assert messages[-1]["role"] == "user"
        root = ElementTree.fromstring(
            "<request>" + messages[-1]["content"] + "</request>"
        )
        assert [node.tag for node in root] == [
            "pending_turns",
            "current_state",
            "runtime_state",
            "state_update_contract",
        ]
        assert [node.attrib for node in root.findall("pending_turns/turn")] == [{"id": "T-1"}]
        assert root.find("runtime_state").text.strip()
        assert root.find("current_state").findall("slot") == []
        assert root.find("state_update_contract").text.strip()
        assert tools == original_tools
        return finish(
            {
                "delete": [],
                "add": [
                    {
                        "subject": "owner",
                        "key": "availability",
                        "value": "busy",
                        "ttl_seconds": 60,
                    }
                ],
            }
        )

    daemon.provider = SimpleNamespace(
        complete=complete, config=SimpleNamespace(api_format="anthropic")
    )
    with patch.object(
        daemon.model_round.context_window,
        "fit",
        side_effect=AssertionError("must preserve source chain"),
    ):
        asyncio.run(daemon._complete_current_state_task("source"))
    assert len(requests) == 1
    assert task_row(daemon.store)["state"] == "completed"
    assert task_row(daemon.store)["payload_json"] is None
    assert daemon.store.current_state.snapshot().slots[0].value == "busy"
    assert [
        tuple(row) for row in daemon.store._db.execute("SELECT * FROM messages")
    ] == before
    assert (
        daemon.store._db.execute("SELECT COUNT(*) FROM current_state_tasks").fetchone()[
            0
        ]
        == 1
    )
    child = task_row(daemon.store)["maintenance_turn_id"]
    assert daemon.store.turn_workflow_kind(child) == "current_state_maintenance"
    assert daemon.store.turn_usage(child)["llm_calls"] == 1
    assert pending_state_source(daemon.store) is None
    asyncio.run(daemon._complete_current_state_task("source"))
    assert len(requests) == 1


def test_invalid_ttl_is_repaired_without_partial_changes(daemon):
    stage(daemon.store)
    count = 0

    async def complete(_system, messages, _tools, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            assert "invalid_ttl" in str(messages[-1])
            assert daemon.store.current_state.snapshot().revision == 0
        return finish(
            {
                "delete": [],
                "add": [
                    {
                        "subject": "owner",
                        "key": "availability",
                        "value": "busy",
                        "ttl_seconds": 86401 if count == 1 else 60,
                    }
                ],
            }
        )

    daemon.provider = SimpleNamespace(
        complete=complete, config=SimpleNamespace(api_format="anthropic")
    )
    asyncio.run(daemon._complete_current_state_task("source"))
    assert count == 2
    assert task_row(daemon.store)["state"] == "completed"


@pytest.mark.parametrize(
    "tool",
    [
        "end_turn",
        "send_bubbles",
        "memory_operation",
        "exec",
        "heartbeat_activity",
        "tool_enable",
    ],
)
def test_maintenance_harness_denies_all_foreground_tools(tool):
    harness = TurnHarness.for_stage("current_state_maintenance")
    assert harness.validate([ToolCall("bad", tool, {})]) is not None


@pytest.mark.parametrize(
    "failure", ["provider", "timeout", "cancel", "source_deleted", "conflict"]
)
def test_failed_maintenance_preserves_source_and_retries_without_overwriting(
    daemon, failure
):
    stage(daemon.store)

    async def complete(*args, **kwargs):
        if failure == "provider":
            raise RuntimeError("provider failed")
        if failure in {"timeout", "cancel"}:
            if failure == "cancel":
                raise asyncio.CancelledError
            await asyncio.sleep(1)
        if failure == "source_deleted":
            with daemon.store._db:
                daemon.store._db.execute("DELETE FROM turns WHERE id='source'")
        if failure == "conflict":
            daemon.store.current_state.apply(
                add=[SlotInput("owner", "location", "home", 60)],
                source_turn_id="other",
                operation_id="other",
                expected_revision=0,
            )
        return finish(
            {
                "delete": [],
                "add": [
                    {
                        "subject": "owner",
                        "key": "availability",
                        "value": "busy",
                        "ttl_seconds": 60,
                    }
                ],
            }
        )

    daemon.provider = SimpleNamespace(
        complete=complete, config=SimpleNamespace(api_format="anthropic")
    )
    with patch(
        "momoi.runtime.workflows.current_state.MAINTENANCE_TIMEOUT_SECONDS", 0.02
    ):
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(daemon._complete_current_state_task("source"))
        else:
            asyncio.run(daemon._complete_current_state_task("source"))
    assert not any(
        slot.key == "availability"
        for slot in daemon.store.current_state.snapshot().slots
    )
    if failure == "source_deleted":
        assert task_row(daemon.store) is None
    else:
        assert task_row(daemon.store)["state"] == "pending"
        assert (
            daemon.store._db.execute(
                "SELECT state FROM turns WHERE id='source'"
            ).fetchone()[0]
            == "completed"
        )
        if failure != "cancel":
            assert daemon.store.current_state_batch_status()["retry_at"] > time.time()


def test_restart_recovers_in_order_and_does_not_reapply_committed_changes(daemon):
    stage(daemon.store)
    stage(daemon.store, "second")
    claimed = daemon.store.claim_current_state_batch("source")
    assert claimed["source_turn_ids"] == ["source", "second"]
    assert daemon.store.current_state_batch_status() is None
    assert daemon.store.claim_current_state_batch("second") is None
    daemon.store.current_state.apply_arguments(
        {"add": [], "delete": []},
        source_turn_id="second",
        operation_id=f"current-state:{claimed['turn_id']}",
        expected_revision=0,
    )
    daemon.store.recover_current_state_tasks()
    assert pending_state_source(daemon.store) == "source"
    assert daemon.store.claim_current_state_batch("source") is None
    assert task_row(daemon.store)["state"] == "completed"
    assert task_row(daemon.store, "second")["state"] == "completed"
    assert (
        daemon.store._db.execute(
            "SELECT state FROM turns WHERE id=?", (claimed["turn_id"],)
        ).fetchone()[0]
        == "completed"
    )
    assert daemon.store.current_state.snapshot().revision == 1
    assert pending_state_source(daemon.store) is None


def test_owner_input_precedes_already_queued_state_maintenance(daemon):
    stage(daemon.store)
    daemon.autonomous.put_nowait(AutonomousJob.current_state("source"))
    event = IncomingMessage("new", "new", "hello", time.time(), time.time())
    daemon.incoming.put_nowait(event)
    assert asyncio.run(daemon._next_work()) == ("owner", event)
    stop = IncomingMessage("stop", "stop", "/stop", time.time(), time.time())
    daemon.incoming.put_nowait(stop)
    assert asyncio.run(daemon._next_work()) == ("owner", stop)


def test_actual_old_schema_upgrade_preserves_foreign_keys_and_supports_new_stage(
    tmp_path,
):
    path = tmp_path / "old.sqlite3"
    schema = files("momoi").joinpath("storage/schema.sql").read_text()
    schema = schema.replace(", 'current_state_maintenance'", "")
    db = sqlite3.connect(path)
    db.executescript(schema)
    db.execute(f"PRAGMA user_version={MIGRATIONS.index(_add_current_state_workflow)}")
    db.execute(
        "INSERT INTO turns(id,kind,workflow_kind,source_ids_json,state,started_at,updated_at) VALUES ('old','owner','owner','[]','completed',1,1)"
    )
    db.commit()
    db.close()
    store = Store(path)
    try:
        assert store.turn_workflow_kind("old") == "owner"
        stage(store)
        task = store.claim_current_state_batch("source")
        assert store.turn_workflow_kind(task["turn_id"]) == "current_state_maintenance"
        assert store._db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        store.close()


@pytest.mark.parametrize("commit_fails", [False, True])
def test_webhook_service_settles_after_commit_or_failure(daemon, commit_fails):
    root = Path(__file__).resolve().parents[1] / "config.example/workflows"
    settled = []

    async def generate(_prompt, turn_id):
        stage(daemon.store, turn_id, "webhook", commit=False)
        return AgentReply([])

    def settle(turn_id):
        settled.append(task_row(daemon.store, turn_id)["state"])

    service = WebhookService(
        WebhookConfig(
            enabled=True,
            token="test",
            workflows=root,
            executors=root / "workflow-executors.yaml",
        ),
        {"channel_url": "ws://napcat.test/ws", "owner_id": "20000"},
        daemon.store,
        generate,
        lambda: None,
        turn_settled=settle,
    )
    plan = bind_workflow(
        service.workflows["event-message"],
        service.executors,
        {"event_prompt": "current input"},
        service.channel_variables,
    )
    daemon.store.create_webhook_run("event-message", "test", plan)
    run = daemon.store.claim_webhook_run()
    if commit_fails:
        with patch.object(
            daemon.store,
            "commit_webhook_reply",
            side_effect=RuntimeError("commit failed"),
        ):
            with pytest.raises(RuntimeError, match="commit failed"):
                asyncio.run(service._execute(run, asyncio.Event()))
        assert settled == ["staged"]
        assert pending_state_source(daemon.store) is None
    else:
        asyncio.run(service._execute(run, asyncio.Event()))
        assert settled == ["pending"]


@pytest.mark.parametrize(
    "count,quiet_seconds,ready",
    [
        (CURRENT_STATE_BATCH_SIZE, CURRENT_STATE_FULL_IDLE_SECONDS - 1, False),
        (CURRENT_STATE_BATCH_SIZE, CURRENT_STATE_FULL_IDLE_SECONDS, True),
        (1, CURRENT_STATE_PARTIAL_IDLE_SECONDS - 1, False),
        (1, CURRENT_STATE_PARTIAL_IDLE_SECONDS, True),
    ],
)
def test_state_batch_uses_full_and_partial_idle_thresholds(
    daemon, count, quiet_seconds, ready
):
    for index in range(count):
        stage(daemon.store, f"source-{index}")

    async def check():
        daemon._last_owner_activity_at = (
            asyncio.get_running_loop().time() - quiet_seconds
        )
        assert daemon._current_state_batch_ready() is ready

    asyncio.run(check())


@pytest.mark.parametrize("count", [6, 7, 8])
def test_state_batch_uses_committed_transcript_including_turns_outside_window(daemon, count):
    for index in range(count):
        stage(daemon.store, f"source-{index}")
        with daemon.store._db:
            daemon.store._db.execute(
                "INSERT INTO messages(turn_id,role,content,created_at,delivery_state,source_event_ids_json) "
                "VALUES (?,'user',?,?,'delivered','[]')",
                (f"source-{index}", f"CURRENT-{index}", time.time()),
            )

    calls = 0

    async def complete(_system, messages, _tools, **kwargs):
        nonlocal calls
        calls += 1
        rendered = str(messages)
        for index in range(count):
            assert rendered.count(f"CURRENT-{index}") == 1
        root = ElementTree.fromstring(
            "<request>" + messages[-1]["content"] + "</request>"
        )
        assert [node.attrib["id"] for node in root.findall("pending_turns/turn")] == [
            f"T-{index + 1}" for index in range(count)
        ]
        bubbles = [
            ElementTree.fromstring(block["text"][block["text"].index("<bubble"):])
            for message in messages[:-1]
            if message["role"] == "user"
            for block in message["content"]
        ]
        assert [bubble.attrib["turn"] for bubble in bubbles] == [
            f"T-{index + 1}" for index in range(count)
        ]
        assert "CURRENT_INPUT" not in rendered
        assert "TOOL_EVIDENCE" not in rendered
        return finish()

    daemon.provider = SimpleNamespace(
        complete=complete, config=SimpleNamespace(api_format="anthropic")
    )
    asyncio.run(daemon._complete_current_state_task("source-0"))

    assert calls == 1
    assert all(
        task_row(daemon.store, f"source-{index}")["state"] == "completed"
        for index in range(count)
    )


@pytest.mark.parametrize("count", [7, 8])
def test_state_batch_includes_turns_arriving_after_threshold(daemon, count):
    for index in range(CURRENT_STATE_BATCH_SIZE):
        stage(daemon.store, f"source-{index}")

    calls = []

    async def complete(_system, messages, _tools, **_kwargs):
        calls.append(copy.deepcopy(messages))
        root = ElementTree.fromstring(
            "<request>" + messages[-1]["content"] + "</request>"
        )
        assert [node.attrib["id"] for node in root.findall("pending_turns/turn")] == [
            f"T-{index + 1}" for index in range(count)
        ]
        return finish()

    daemon.provider = SimpleNamespace(
        complete=complete, config=SimpleNamespace(api_format="anthropic")
    )

    async def run():
        loop = asyncio.get_running_loop()
        daemon._last_owner_activity_at = (
            loop.time() - CURRENT_STATE_FULL_IDLE_SECONDS
        )
        assert daemon._current_state_batch_ready()
        # Additional completed Turns before execution belong to the same claim.
        for index in range(CURRENT_STATE_BATCH_SIZE, count):
            stage(daemon.store, f"source-{index}")
        await daemon._complete_current_state_task("source-0")

        assert daemon.store.current_state_batch_status() is None
        assert not daemon._current_state_batch_ready()
        rows = [task_row(daemon.store, f"source-{index}") for index in range(count)]
        assert all(row["state"] == "completed" for row in rows)
        assert len({row["maintenance_turn_id"] for row in rows}) == 1

    asyncio.run(run())
    assert len(calls) == 1


def test_retry_uses_latest_snapshot_and_then_advances_queue(daemon):
    stage(daemon.store)
    stage(daemon.store, "second")
    claimed = daemon.store.claim_current_state_batch("source")
    daemon.store.release_current_state_batch(
        claimed["source_turn_ids"], claimed["turn_id"], "provider"
    )
    assert daemon.store.current_state_batch_status()["retry_at"] > time.time()
    daemon.store.current_state.apply(
        add=[SlotInput("owner", "location", "home", 60)],
        source_turn_id="newer",
        operation_id="newer",
        expected_revision=0,
    )
    with daemon.store._db:
        daemon.store._db.execute(
            "UPDATE current_state_tasks SET retry_at=0"
        )

    async def complete(_system, messages, _tools, **kwargs):
        assert ">home</slot>" in messages[-1]["content"]
        return finish()

    daemon.provider = SimpleNamespace(
        complete=complete, config=SimpleNamespace(api_format="anthropic")
    )
    asyncio.run(daemon._complete_current_state_task("source"))
    assert task_row(daemon.store)["attempts"] == 2
    assert task_row(daemon.store)["state"] == "completed"
    assert task_row(daemon.store, "second")["state"] == "completed"
    assert pending_state_source(daemon.store) is None


@pytest.mark.parametrize(
    "kind", sorted(set(TURN_HARNESS_SPECS) - {"current_state_maintenance"})
)
def test_visible_state_tool_cannot_execute_outside_maintenance(daemon, kind):
    assert "current_state_finish" in {
        spec["name"] for spec in daemon.tool_surface.conversation_specs()
    }
    harness = TurnHarness.for_stage(
        kind,
        permitted_tool_names=frozenset({"current_state_finish"}),
    )
    assert (
        harness.validate(
            [ToolCall("state", "current_state_finish", {"add": [], "delete": []})]
        )
        == "tool_not_allowed"
    )


@pytest.mark.parametrize("name", ["end_turn", "mcp__test__lookup"])
def test_maintenance_preserves_visible_tools_but_rejects_their_execution(daemon, name):
    stage(daemon.store)
    tools_before = json.loads(task_row(daemon.store)["payload_json"])["tools"]
    count = 0

    async def complete(_system, messages, tools, **kwargs):
        nonlocal count
        count += 1
        assert tools == tools_before
        if count == 1:
            return response(ToolCall("forbidden", name, {}))
        assert "tool_not_allowed" in str(messages[-1])
        return finish()

    daemon.provider = SimpleNamespace(
        complete=complete, config=SimpleNamespace(api_format="anthropic")
    )
    with patch.object(
        daemon.tool_surface,
        "conversation_specs",
        side_effect=AssertionError("must use saved schemas"),
    ):
        asyncio.run(daemon._complete_current_state_task("source"))
    assert count == 2
    assert task_row(daemon.store)["state"] == "completed"
