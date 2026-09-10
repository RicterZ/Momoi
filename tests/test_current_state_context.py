import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.models import AgentReply, IncomingMessage, ToolCall, TurnDraft
from momoi.runtime import MomoiDaemon
from momoi.runtime.context.current_state import pack_current_turn_context
from momoi.semantic.episode_cue_verifier import verify_episode_cues
from momoi.storage.current_state import CurrentStateManager, SlotInput
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
            timezone="Asia/Shanghai",
            transcript_turns_min=4,
            transcript_turns_max=4,
            episode_raw_tail_turns=2,
            memory_results=6,
            log_level="INFO",
        )
    )
    yield value
    value.store.close()


def seed(daemon):
    clock = [1000.0]
    manager = CurrentStateManager(daemon.store._db, clock=lambda: clock[0])
    daemon.store.current_state = manager
    change = manager.apply(
        add=[
            SlotInput('owner "<&', "availability", "STATE_ONLY </slot> & <fake/>", 60),
            SlotInput("owner", "location", "EXPIRED_STATE", 1),
        ],
        source_turn_id="private-source-turn",
        operation_id="seed",
        expected_revision=0,
    )
    clock[0] = 1001
    return clock, change.added[0]


class ContextCaptured(BaseException):
    pass


@pytest.mark.parametrize(
    "stage", ["owner", "goal", "heartbeat", "webhook", "reply_followup"]
)
def test_real_workflows_inject_only_current_input_and_preserve_history(daemon, stage):
    clock, slot = seed(daemon)
    daemon.store.commit_turn([], "HISTORY_ONLY", AgentReply([]), turn_id="old")
    before = [tuple(row) for row in daemon.store._db.execute("SELECT * FROM messages")]
    history = daemon.store.current_state.history()
    captured = {}

    async def capture(system, messages, tools, *args, **kwargs):
        captured.update(messages=messages, system=system, tools=tools)
        raise ContextCaptured

    daemon._run_tool_loop = capture
    if stage == "owner":
        at = datetime.now(timezone.utc).timestamp()
        event = IncomingMessage("new", "new", "CURRENT_INPUT", at, at)
        work = daemon._complete_batch([event], "owner-context")
    elif stage == "heartbeat":
        work = daemon._complete_heartbeat("heartbeat-context", owner_event_revision=0)
    elif stage == "webhook":
        work = daemon._complete_webhook_turn("CURRENT_WEBHOOK", "webhook-context")
    elif stage == "reply_followup":
        with daemon.store._db:
            daemon.store._db.execute("""UPDATE self_state SET pending_reply_turn_id='old',
                pending_reply_expectation='answer', pending_reply_since=1000,
                pending_reply_last_reason='follow up', pending_reply_channel='napcat',
                pending_reply_next_check_at=1060 WHERE id=1""")
        work = daemon._complete_reply_wait("followup-context", owner_event_revision=0)
    else:
        draft = TurnDraft()
        result = daemon.agenda_tools.execute(
            ToolCall(
                "goal",
                "goal_create",
                {
                    "title": "Check",
                    "success_criteria": "Checked",
                    "next_action": "Check",
                    "next_review_at": (
                        datetime.now(timezone.utc) + timedelta(hours=1)
                    ).isoformat(),
                },
            ),
            draft,
            authority="owner",
            source_event_id="event",
        )
        assert result["ok"]
        daemon.store.commit_goal_draft(draft)
        work = daemon._complete_goal(result["goal"]["id"], "goal-context")
    with pytest.raises(ContextCaptured):
        asyncio.run(work)
    messages = captured["messages"]
    assert "STATE_ONLY" not in json.dumps(messages[:-1])
    assert "HISTORY_ONLY" in json.dumps(messages[:-1])
    assert "STATE_ONLY" not in json.dumps(captured["system"])
    assert "STATE_ONLY" not in json.dumps(captured["tools"])
    text = "".join(block.get("text", "") for block in messages[-1]["content"])
    assert text.startswith("<current_state>")
    root = ElementTree.fromstring("<input>" + text + "</input>")
    entries = root.findall("current_state/slot")
    assert len(entries) == 1
    assert entries[0].attrib == {
        "id": slot.id,
        "subject": slot.subject,
        "key": slot.key,
        "expires_at": daemon.store.context_timestamp(slot.expires_at),
    }
    assert entries[0].text == slot.value
    assert list(entries[0]) == []
    assert "EXPIRED_STATE" not in text
    assert "private-source-turn" not in text
    assert [
        tuple(row) for row in daemon.store._db.execute("SELECT * FROM messages")
    ] == before
    assert daemon.store.current_state.history() == history


def test_owner_update_refreshes_state_and_explicitly_clears_expired_snapshot(daemon):
    clock, _ = seed(daemon)
    event = IncomingMessage("new", "new", "NEW_INPUT", 1002, 1002)
    baseline = daemon.owner_context_baseline()
    before = daemon._owner_update_message([event], daemon.channel, baseline)
    assert "STATE_ONLY" in json.dumps(before)
    clock[0] = 1060
    after = daemon._owner_update_message([event], daemon.channel, baseline)
    text = after["content"][0]["text"]
    assert text.startswith("<current_state />")
    assert "STATE_ONLY" not in text
    assert "NEW_INPUT" in json.dumps(after)


def test_empty_current_state_omits_section_and_reading_never_renews_ttl(daemon):
    clock, slot = seed(daemon)
    assert "STATE_ONLY" in pack_current_turn_context(daemon.store, "owner")
    clock[0] = slot.expires_at
    assert (
        pack_current_turn_context(daemon.store, "owner", ("runtime_state", "<time />"))
        == "<runtime_state>\n<time />\n</runtime_state>"
    )
    assert daemon.store.current_state.snapshot().revision == 1


@pytest.mark.parametrize(
    "stage",
    [
        "reflection",
        "episode_consolidate",
        "episode_anneal",
        "episode_cue_admit",
        "memory_operation",
        "memory_maintenance",
        "unknown",
    ],
)
def test_private_processing_cannot_use_live_state_context(daemon, stage):
    seed(daemon)
    with pytest.raises(ValueError, match="current state is not available"):
        pack_current_turn_context(daemon.store, stage)


def test_cues_audit_receives_only_cues_and_claims(daemon):
    seed(daemon)
    claims = [{"message_id": 17, "role": "user", "quote": "tired"}]
    cues = [{"text": "When recalling owner tiredness", "evidence_message_ids": [17]}]

    async def complete(_system, messages, _tools, **_kwargs):
        from xml.etree.ElementTree import fromstring
        request = fromstring(messages[0]["content"])
        assert [node.tag for node in request] == ["sources", "cues"]
        assert request.find("sources/message").get("source") == "OWNER"
        assert request.findtext("sources/message/quote") == claims[0]["quote"]
        assert request.find("cues/cue").get("sources") == "17"
        assert request.findtext("cues/cue") == cues[0]["text"]
        return SimpleNamespace(
            tool_calls=[
                ToolCall("admit", "episode_cue_admit", {"supported_indices": [0]})
            ]
        )

    assert (
        asyncio.run(
            verify_episode_cues(SimpleNamespace(complete=complete), cues, claims)
        )
        == cues
    )
