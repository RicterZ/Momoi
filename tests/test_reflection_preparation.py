import asyncio
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from momoi.config.models import EpisodeAnnealingConfig
from momoi.models import ToolCall
from momoi.runtime import MomoiDaemon
from tests.test_episode_annealing import annealing_items, config


@pytest.fixture
def daemon(tmp_path):
    value = MomoiDaemon(replace(config(str(tmp_path)), timezone="Asia/Shanghai"))
    yield value
    value.store.close()


def add_turn(daemon, name, at):
    with daemon.store._db:
        daemon.store._db.execute(
            "INSERT INTO turns(id,kind,state,source_ids_json,started_at,updated_at) "
            "VALUES (?,'owner','completed','[]',?,?)", (name, at, at),
        )
        daemon.store._db.execute(
            "INSERT INTO messages(turn_id,role,content,created_at,delivery_state,source_event_ids_json) "
            "VALUES (?,'user',?,?,'delivered','[]')", (name, name + "完成项目", at),
        )


def day_window():
    start = datetime(2026, 9, 8, tzinfo=ZoneInfo("Asia/Shanghai"))
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def test_reflection_reads_material_after_small_batch_and_summary(daemon):
    start, end = day_window()
    for name, at in [("older", start - 1), ("day-1", start + 1), ("day-2", end - 1), ("later", end)]:
        add_turn(daemon, name, at)
    assert daemon.store.claim_episode_consolidation_candidate() is None
    daemon.store.claim_manual_reflection(start + 3600)
    stages = []

    async def workflow(_system, messages, _tools, turn_id, workflow):
        stages.append(workflow.stage)
        if workflow.stage == "episode_consolidate":
            ids = re.findall(r'<turn id="([^"]+)"', messages[-1]["content"])
            assert ids == ["T-1", "T-2"]
            result = await workflow.execute_tool(ToolCall("classify", "episode_classify_turns", {
                "decisions": [{"action": "new", "key": "project", "title": "我和老师完成项目",
                               "turn_ids": ids, "topics": [], "entities": [], "open_loops": [], "salience": 0.5}],
            }))
            assert result["ok"]
            result = await workflow.execute_tool(ToolCall("done", "episode_consolidation_finish", {}))
        elif workflow.stage == "episode_anneal":
            items = annealing_items(messages[0]["content"], "new_messages", "Message")
            assert [item["turn_id"] for item in items] == ["day-1", "day-2"]
            result = await workflow.execute_tool(ToolCall("summary", "episode_summary_finish", {
                "claims": [{key: item[key] for key in ("message_id", "turn_id", "ordinal")} |
                           {"quote": item["content"]} for item in items],
                "narrative_summary": "我和老师完成了当天项目。",
                "emotional_context": {"owner": "", "assistant": "", "tone": ""}, "outcomes": [],
            }))
        else:
            assert workflow.stage == "reflection"
            assert "我和老师完成了当天项目。" in json.dumps(messages, ensure_ascii=False)
            result = await workflow.execute_tool(ToolCall("reflect", "reflection_finish", {
                "summary": "当天项目已完成。", "memories": [], "conversation_actions": [],
            }))
        assert result["ok"]
        return workflow.completion_result()

    daemon._run_agent_workflow = workflow
    asyncio.run(daemon._complete_reflection("2026-09-08", "reflection-test"))
    assert stages == ["episode_consolidate", "episode_anneal", "reflection"]
    assert daemon.store.episodes_for_turns(["older", "later"]) == {}
    assert daemon.store.reflection("2026-09-08")["state"] == "completed"


def test_summary_flush_respects_date_boundary_and_retry(daemon):
    start, end = day_window()
    for name, at in [("past", start - 1), ("today", start + 10), ("tomorrow", end)]:
        add_turn(daemon, name, at)
    daemon.store.create_episode("跨日项目", episode_id="project")
    for name in ["past", "today", "tomorrow"]:
        daemon.store.link_turn_to_episode("project", name)
    assert daemon.store.claim_episode_annealing_candidate(2, 4000) is None
    candidate = daemon.store.claim_episode_annealing_candidate(2, 4000, window=(start, end))
    assert [message["turn_id"] for message in candidate["messages"]] == ["past", "today"]
    assert candidate["through_ordinal"] == 2
    daemon.store.release_episode_annealing("project")
    assert daemon.store.claim_episode_annealing_candidate(2, 4000, window=(start, end)) is None


def test_preparation_skips_disabled_and_does_not_retry_deferred_batch(daemon):
    start, end = day_window()
    add_turn(daemon, "pending", start + 1)
    calls = []

    async def defer(candidate):
        calls.append(candidate)
        daemon.store.apply_episode_consolidation(["pending"], [
            {"action": "defer", "turn_ids": ["pending"], "reason": "等待后文"},
        ])
        return True

    daemon._consolidate_episode_turns = defer
    daemon.config = replace(daemon.config, episode_annealing=EpisodeAnnealingConfig(enabled=False))
    asyncio.run(daemon._prepare_reflection_episodes("2026-09-08"))
    assert calls == []
    daemon.config = replace(daemon.config, episode_annealing=EpisodeAnnealingConfig())
    asyncio.run(daemon._prepare_reflection_episodes("2026-09-08"))
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [RuntimeError("provider failed"), TimeoutError()])
def test_preparation_failure_keeps_original_reflection_material(daemon, failure, caplog):
    start, _ = day_window()
    add_turn(daemon, "pending", start + 1)
    daemon.store.claim_manual_reflection(start + 3600)
    daemon._consolidate_episode_turns = AsyncMock(side_effect=failure)

    async def reflect(_system, messages, _tools, turn_id, workflow):
        assert workflow.stage == "reflection"
        assert "pending完成项目" in json.dumps(messages, ensure_ascii=False)
        result = await workflow.execute_tool(ToolCall("finish", "reflection_finish", {
            "summary": "仍能根据原始材料复盘", "memories": [], "conversation_actions": [],
        }))
        assert result["ok"]
        return workflow.completion_result()

    daemon._run_agent_workflow = reflect
    asyncio.run(daemon._complete_reflection("2026-09-08", "reflection-test"))
    assert "reflection_episode_preparation_failed" in caplog.text
    assert "pending完成项目" in daemon.store.reflection_source("2026-09-08", 4000)["text"]
    assert daemon.store.reflection("2026-09-08")["state"] == "completed"


def test_cross_midnight_turn_is_not_partially_classified(daemon):
    start, end = day_window()
    add_turn(daemon, "crossing", end - 1)
    with daemon.store._db:
        daemon.store._db.execute(
            "INSERT INTO messages(turn_id,role,content,created_at,delivery_state,source_event_ids_json) "
            "VALUES ('crossing','assistant','跨日回复',?,'delivered','[]')", (end + 1,),
        )
    assert daemon.store.claim_episode_consolidation_candidate(minimum=1) is not None
    assert daemon.store.claim_episode_consolidation_candidate(minimum=1, window=(start, end)) is None


def test_preparation_waits_for_background_cleanup_and_has_total_timeout(daemon, caplog):
    start, _ = day_window()
    add_turn(daemon, "pending", start + 1)
    daemon.config = replace(daemon.config, episode_annealing=EpisodeAnnealingConfig(max_seconds=0.03))
    cleaned = []

    async def exercise():
        started = asyncio.Event()

        async def background():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)

        async def stalled(candidate):
            assert cleaned == [True]
            await asyncio.Event().wait()

        daemon._active_annealing = asyncio.create_task(background())
        await started.wait()
        daemon._consolidate_episode_turns = stalled
        await asyncio.wait_for(daemon._prepare_reflection_episodes("2026-09-08"), timeout=1)
        assert daemon._active_annealing.cancelled()

    asyncio.run(exercise())
    assert "reflection_episode_preparation_failed" in caplog.text


def test_preparation_propagates_cancellation(daemon):
    start, _ = day_window()
    add_turn(daemon, "pending", start + 1)
    daemon._consolidate_episode_turns = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(daemon._prepare_reflection_episodes("2026-09-08"))
