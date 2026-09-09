import asyncio
import copy
import json
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
from jsonschema import Draft202012Validator

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.models import ToolCall, TurnDraft, ProviderResponse
from momoi.runtime import MomoiDaemon
from momoi.runtime.agent.harness import TurnHarness, TURN_HARNESS_SPECS
from momoi.runtime.agent.runtime_tools import record_heartbeat_activity
from momoi.runtime.context.presentation import heartbeat_self_state_lines
from momoi.runtime.tool_contracts.conversation import HEARTBEAT_ACTIVITY_TOOL_SPEC
from momoi.storage import Store
from momoi.storage.migrations import MIGRATIONS, _restore_last_heartbeat_activity
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


def activity(**overrides):
    return ToolCall(
        "activity",
        "heartbeat_activity",
        {"activity": "reading", "result": "finished", "next_check_minutes": 30, "reason": "rest", **overrides},
    )


def test_visible_tool_schema_is_stable_and_permissions_are_separate(daemon):
    canonical = copy.deepcopy(HEARTBEAT_ACTIVITY_TOOL_SPEC)
    original = daemon.tool_surface.conversation_specs()
    for stage in ("owner", "goal", "heartbeat", "webhook", "reply_followup"):
        specs = daemon.tool_surface.conversation_specs()
        assert specs == original
        assert "goal_review" in {tool["name"] for tool in specs}
        assert ("goal_review" in daemon.tool_surface.permitted_names(stage)) == (stage == "goal")
        assert (
            next(tool for tool in specs if tool["name"] == "heartbeat_activity")
            == canonical
        )
        assert ("heartbeat_activity" in daemon.tool_surface.permitted_names(stage)) == (
            stage == "heartbeat"
        )
    next(tool for tool in specs if tool["name"] == "heartbeat_activity")[
        "description"
    ] = "mutated caller copy"
    assert HEARTBEAT_ACTIVITY_TOOL_SPEC == canonical


@pytest.mark.parametrize(
    "stage", [stage for stage in TURN_HARNESS_SPECS if stage != "heartbeat"]
)
def test_harness_denies_activity_even_with_broad_permissions(stage):
    harness = TurnHarness.for_stage(
        stage, permitted_tool_names=frozenset({"heartbeat_activity"})
    )
    assert harness.validate([activity()]) == "tool_not_allowed"
    draft = TurnDraft()
    assert (
        record_heartbeat_activity(activity(), heartbeat_turn=False, draft=draft)["ok"]
        is False
    )
    assert draft.heartbeat_activity is None


def test_heartbeat_requires_successful_activity_and_reset_clears_gate():
    harness = TurnHarness.for_stage("heartbeat")
    with pytest.raises(ValueError, match="heartbeat_activity"):
        harness.validate_surface({"heartbeat_begin", "end_turn"})
    assert harness.validate([activity()]) == "heartbeat_begin_must_be_first_and_alone"
    harness.accept("heartbeat_begin")
    end = ToolCall("end", "end_turn", {})
    harness.observe_calls([activity()])
    assert harness.validate([end]) == "heartbeat_activity_required_before_end_turn"
    assert harness.validate([activity(), end]) == "end_turn_must_be_alone"
    harness.accept("heartbeat_activity")
    assert harness.validate([end]) is None
    harness.reset()
    harness.accept("heartbeat_begin")
    assert harness.validate([end]) == "heartbeat_activity_required_before_end_turn"


@pytest.mark.parametrize("minutes,valid", [(3, True), (10, True), (2, False), (11, False), (True, False), (3.5, False)])
def test_heartbeat_schedule_respects_configured_limits_without_mutating_on_error(minutes, valid):
    draft = TurnDraft()
    baseline = record_heartbeat_activity(activity(next_check_minutes=5), heartbeat_turn=True, draft=draft,
                                         minimum_seconds=180, maximum_seconds=600)
    assert baseline["ok"]
    before = copy.deepcopy(draft.heartbeat_activity)
    result = record_heartbeat_activity(activity(next_check_minutes=minutes), heartbeat_turn=True, draft=draft,
                                       minimum_seconds=180, maximum_seconds=600)
    assert result["ok"] is valid
    if not valid:
        assert draft.heartbeat_activity == before


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"activity": "", "result": ""},
        {"activity": " ", "result": ""},
        {"activity": "a" * 301, "result": ""},
        {"activity": "rest", "result": None},
        {"activity": "rest", "result": "r" * 2001},
        {"activity": "rest", "result": "", "decision": "unchanged"},
    ],
)
def test_invalid_activity_does_not_overwrite_valid_draft(args):
    draft = TurnDraft()
    assert record_heartbeat_activity(activity(), heartbeat_turn=True, draft=draft)["ok"]
    before = copy.deepcopy(draft.heartbeat_activity)
    bad = ToolCall("bad", "heartbeat_activity", args)
    assert not record_heartbeat_activity(bad, heartbeat_turn=True, draft=draft)["ok"]
    assert draft.heartbeat_activity == before


@pytest.mark.parametrize("cancel", [False, True])
def test_real_heartbeat_retries_gate_and_commits_only_on_completion(daemon, cancel):
    before = daemon.store.self_state()
    begin = ToolCall(
        "begin",
        "heartbeat_begin",
        {
            "activity": "reading",
            "mode": "rest",
            "recall_mode": "skip",
            "recall_queries": [],
            "tool_groups": [],
            "strategy": [],
        },
    )
    end = ToolCall(
        "end",
        "end_turn",
        {
            "mood": {"decision": "unchanged"},
            "reply_wait": {"wait": False},
        },
    )
    invalid = activity(activity="")
    calls = [begin, invalid, end, activity(), end]

    async def complete(_system, messages, tools, **_kwargs):
        assert calls, "unexpected retry"
        if len(calls) == 2:
            assert "heartbeat_activity_required_before_end_turn" in str(messages[-1])
        if len(calls) == 1:
            assert daemon.store.self_state()["activity"] == before["activity"]
            if cancel:
                raise asyncio.CancelledError
        call = calls.pop(0)
        schema = next(
            spec["input_schema"] for spec in tools if spec["name"] == call.name
        )
        assert Draft202012Validator(schema).is_valid(call.arguments) == (
            call is not invalid
        )
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

    daemon.provider = SimpleNamespace(
        complete=complete, config=SimpleNamespace(api_format="anthropic")
    )
    daemon.store.begin_turn("heartbeat-test", "heartbeat", [])
    work = daemon._complete_heartbeat("heartbeat-test", owner_event_revision=0)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(work)
        assert daemon.store.self_state()["activity"] == before["activity"]
        assert (
            daemon.store.self_state()["last_heartbeat_at"]
            == before["last_heartbeat_at"]
        )
    else:
        asyncio.run(work)
        state = daemon.store.self_state()
        assert (state["activity"], state["activity_result"]) == ("reading", "finished")
        assert state["last_heartbeat_at"] is not None


@pytest.mark.parametrize(
    "record", [None, "Activity: read\nResult: finished", "unrecognized record"]
)
def test_upgrade_recovers_exact_heartbeat_not_owner_overwrite(tmp_path, record):
    path = tmp_path / "old.sqlite3"
    store = Store(path)
    with store._db:
        store._db.execute(
            "UPDATE self_state SET activity='owner overwrite', activity_result='stale', last_heartbeat_at=100, next_heartbeat_at=200, mood_state='happy' WHERE id=1"
        )
        if record is not None:
            store._db.execute(
                "INSERT INTO messages(turn_id,role,content,created_at,delivery_state,source_event_ids_json) VALUES ('beat','assistant',?,100,'internal',?)",
                (record, json.dumps(["heartbeat-record:beat"])),
            )
        store._db.execute(f"PRAGMA user_version={MIGRATIONS.index(_restore_last_heartbeat_activity)}")
    store.close()
    store = Store(path)
    try:
        state = store.self_state()
        expected = (
            ("read", "finished")
            if record and record.startswith("Activity: ")
            else ("", "")
        )
        assert (state["activity"], state["activity_result"]) == expected
        assert state["next_heartbeat_at"] == 200
        assert state["mood_state"] == "happy"
        root = ElementTree.fromstring(
            "<state>"
            + heartbeat_self_state_lines(store.self_state_context())
            + "</state>"
        )
        assert (root.find("last_heartbeat_activity") is not None) == bool(expected[0])
        assert root.find("activity") is None
    finally:
        store.close()
