import asyncio
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from momoi.config.models import EpisodeAnnealingConfig, ReflectionConfig
from momoi.models import ToolCall
from momoi.runtime import MomoiDaemon
from momoi.runtime.transcript.reflection import reflection_transcript
from momoi.storage.reflection.reflection_values import reflection_window
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
    start = datetime(2026, 9, 8, 3, tzinfo=ZoneInfo("Asia/Shanghai"))
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def test_reflection_reads_material_after_small_batch_and_summary(daemon):
    start, end = day_window()
    for name, at in [("older", start - 1), ("day-1", start + 1), ("day-2", end - 1), ("later", end)]:
        add_turn(daemon, name, at)
    assert daemon.store.claim_episode_consolidation_candidate() is None
    daemon.store.claim_due_reflection(ReflectionConfig(enabled=True), end + 3600)
    stages = []

    async def workflow(_system, messages, _tools, turn_id, workflow):
        stages.append(workflow.stage)
        if workflow.stage == "episode_consolidate":
            ids = re.findall(r'<turn id="([^"]+)"', messages[-1]["content"])
            assert ids == ["T-1", "T-2"]
            result = await workflow.execute_tool(ToolCall("classify", "episode_classify_turns", {
                "decisions": [{"action": "new", "key": "project", "title": "我和老师完成项目",
                               "turn_ids": ids, "topics": [], "entities": [], "open_loops": []}],
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
    start, end = day_window()
    add_turn(daemon, "pending", start + 1)
    daemon.store.claim_due_reflection(ReflectionConfig(enabled=True), end + 3600)
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
    rows = daemon.store.conversation_messages_for_turns(["pending"])
    assert rows[0]["content"] == "pending完成项目"
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


@pytest.mark.parametrize("at", ["03:00", "04:30", "00:00"])
def test_reflection_uses_configured_window_and_complete_transcript(daemon, at):
    daemon.config = replace(
        daemon.config,
        reflection=ReflectionConfig(at=at),
        episode_annealing=EpisodeAnnealingConfig(enabled=False),
    )
    start, end = reflection_window("2026-09-08", at, daemon.store.timezone)
    add_turn(daemon, "before-window", start - 1)
    count = daemon.config.transcript_turns_max + 10
    for index in range(count):
        add_turn(daemon, f"in-window-{index:02d}", start + index)
    add_turn(daemon, "last-in-window", end - 1)
    add_turn(daemon, "after-window", end)
    with daemon.store._db:
        daemon.store._db.execute(
            "INSERT INTO messages(turn_id,role,content,created_at,delivery_state,source_event_ids_json) "
            "VALUES ('last-in-window','assistant','outside reply',?,'delivered','[]')", (end,),
        )
    claimed = daemon.store.claim_due_reflection(ReflectionConfig(enabled=True, at=at), end)
    assert claimed["local_date"] == "2026-09-08"
    source = daemon.store.reflection_source("2026-09-08", at=at)
    assert (source["start_at"], source["end_at"]) == (start, end)
    rows = daemon.store.conversation_messages_for_turns(None, window=(start, end))
    assert len(rows) == count + 1
    expected_messages = reflection_transcript(daemon.store, rows, (start, end))

    async def reflect(_system, messages, _tools, turn_id, workflow):
        assert workflow.preserve_transcript
        assert messages[:-1] == expected_messages
        final_input = messages[-1]["content"][0]["text"]
        for row in rows:
            assert row["content"] not in final_input
        for section in (
            "daily_reflection_record", "tool_timeline", "self_state", "reflection_scope",
            "topic_timeline", "mutation_timeline", "episode_directory",
        ):
            assert f"<{section}>" not in final_input
        result = await workflow.execute_tool(ToolCall("finish", "reflection_finish", {
            "summary": "完成复盘", "memories": [], "conversation_actions": [],
        }))
        assert result["ok"]
        return workflow.completion_result()

    daemon._run_agent_workflow = reflect
    asyncio.run(daemon._complete_reflection("2026-09-08", "reflection-test"))
    assert daemon.store.reflection("2026-09-08")["state"] == "completed"


def test_episode_timeline_selects_same_period_without_extracts(daemon):
    start, end = reflection_window("2026-09-08", "04:30", daemon.store.timezone)
    add_turn(daemon, "linked-turn", start)
    for name, created, updated in [
        ("before", start - 10, start - 1), ("after", end, end + 1),
        ("created", start, end + 1), ("updated", start - 10, end - 1),
        ("linked", start - 10, end + 1),
    ] + [(f"extra-{i:02d}", start + i, start + i) for i in range(20)]:
        daemon.store.create_episode(name, episode_id=name)
        with daemon.store._db:
            daemon.store._db.execute(
                "UPDATE conversation_episodes SET created_at=?, updated_at=?, "
                "narrative_summary=?, working_summary=? WHERE id=?",
                (created, updated, f"summary:{name}", "verbatim-chat-extract", name),
            )
    daemon.store.link_turn_to_episode("linked", "linked-turn")
    timeline = daemon.store.reflection_source("2026-09-08", at="04:30")["episode_timeline"]
    for name in ["created", "updated", "linked", *[f"extra-{i:02d}" for i in range(20)]]:
        assert f"id={name} " in timeline
        assert f"summary:{name}" in timeline
    for name in ["before", "after"]:
        assert f"id={name} " not in timeline
    assert "verbatim-chat-extract" not in timeline
    assert "linked-turn完成项目" not in timeline
    assert "verbatim-chat-extract" not in daemon.store.open_conversation_inventory_context()


@pytest.mark.parametrize("offset,local_date", [(-1, "2026-09-08"), (0, "2026-09-09"), (3600, "2026-09-09")])
def test_manual_reflection_selects_current_period(daemon, offset, local_date):
    config = ReflectionConfig(enabled=True, at="04:30")
    _, end = reflection_window("2026-09-08", config.at, daemon.store.timezone)
    manual = daemon.store.claim_manual_reflection(end + offset, at=config.at)
    assert manual["local_date"] == local_date
    assert manual["scheduled_at"] == end + offset


def test_manual_reflection_does_not_skip_later_full_reflection(daemon):
    config = ReflectionConfig(enabled=True, at="04:30")
    start, end = reflection_window("2026-09-08", config.at, daemon.store.timezone)
    manual = daemon.store.claim_manual_reflection(start + 3600, at=config.at)
    assert manual["local_date"] == "2026-09-08"
    # Do not claim over an in-flight manual run when the scheduled boundary passes.
    assert daemon.store.claim_due_reflection(config, end) is None
    assert daemon.store.next_reflection_due_at(config, end) is None
    daemon.store.restore_completed_reflection_claim("2026-09-08")
    assert daemon.store.next_reflection_due_at(config, end) == end
    scheduled = daemon.store.claim_due_reflection(config, end + 10)
    assert scheduled["local_date"] == "2026-09-08"
    assert scheduled["scheduled_at"] == end
    daemon.store.restore_completed_reflection_claim("2026-09-08")
    assert daemon.store.claim_due_reflection(config, end + 20) is None
    assert daemon.store.next_reflection_due_at(config, end + 20) == end + 86400


@pytest.mark.parametrize("at,offset", [("03:00", 0), ("03:00", 11 * 3600), ("04:30", 23 * 3600)])
def test_manual_reflection_transcript_and_timelines_stop_at_command(daemon, at, offset):
    daemon.config = replace(
        daemon.config, reflection=ReflectionConfig(at=at),
        episode_annealing=EpisodeAnnealingConfig(enabled=False),
    )
    start, _ = reflection_window("2026-09-08", at, daemon.store.timezone)
    trigger = start + offset
    for name, timestamp in [("before", start - 1), ("inside", start), ("after", trigger)]:
        add_turn(daemon, name, timestamp)
        daemon.store.create_episode(name, episode_id=name)
        with daemon.store._db:
            daemon.store._db.execute(
                "UPDATE conversation_episodes SET created_at=?, updated_at=?, narrative_summary=? WHERE id=?",
                (timestamp, timestamp, "summary:" + name, name),
            )
        daemon.store.append_turn_journal(name, "final", {
            "mood_change": {"state": name, "intensity": 0.5, "cause": "test"},
        }, created_at=timestamp)
    manual = daemon.store.claim_manual_reflection(trigger, at=at)
    assert manual["local_date"] == "2026-09-08"
    prepare = AsyncMock()
    daemon._prepare_reflection_episodes = prepare

    async def reflect(_system, messages, _tools, turn_id, workflow):
        transcript = json.dumps(messages[:-1], ensure_ascii=False)
        final_input = messages[-1]["content"][0]["text"]
        episode_timeline = re.search(r"<episode_timeline>\n(.*?)\n</episode_timeline>", final_input, re.S)[1]
        mood_timeline = re.search(r"<mood_timeline>\n(.*?)\n</mood_timeline>", final_input, re.S)[1]
        assert ("inside完成项目" in transcript) == (offset > 0)
        assert ("id=inside " in episode_timeline) == (offset > 0)
        assert ("state=inside " in mood_timeline) == (offset > 0)
        for name in ["before", "after"]:
            assert name + "完成项目" not in transcript
            assert f"id={name} " not in episode_timeline
            assert f"state={name} " not in mood_timeline
        result = await workflow.execute_tool(ToolCall("finish", "reflection_finish", {
            "summary": "手动复盘", "memories": [], "conversation_actions": [],
        }))
        assert result["ok"]
        return workflow.completion_result()

    daemon._run_agent_workflow = reflect
    asyncio.run(daemon._complete_reflection("2026-09-08", "manual-test"))
    prepare.assert_awaited_once_with("2026-09-08", at=at, end_at=trigger)


def test_reflection_window_keeps_wall_clock_time_across_dst():
    timezone = ZoneInfo("America/New_York")
    start, end = reflection_window("2026-03-07", "03:00", timezone)
    assert datetime.fromtimestamp(start, timezone).hour == 3
    assert datetime.fromtimestamp(end, timezone).hour == 3
    assert end - start == 23 * 3600


def test_webhook_transcript_uses_reception_time_for_window(daemon):
    start, end = day_window()
    for name, received, archived in [("included", start, end), ("excluded", start - 1, start)]:
        with daemon.store._db:
            daemon.store._db.execute(
                "INSERT INTO webhook_runs(id,workflow_id,plan_json,state,created_at,updated_at) "
                "VALUES (?,'test','{}','succeeded',?,?)", (name, received, archived),
            )
            daemon.store._db.execute(
                "INSERT INTO webhook_steps(run_id,step_index,step_id,kind,state) "
                "VALUES (?,0,'step','message','succeeded')", (name,),
            )
            daemon.store._db.execute(
                "INSERT INTO messages(turn_id,role,content,created_at,delivery_state,source_event_ids_json) "
                "VALUES (?,'event',?,?,'internal','[]')", (f"webhook:{name}:0", name, archived),
            )
    rows = daemon.store.conversation_messages_for_turns(None, window=(start, end))
    assert [(row["content"], row["created_at"]) for row in rows] == [("included", start)]


def test_text_reflection_excludes_recalled_background_from_daily_evidence(daemon):
    daemon.config = replace(daemon.config, episode_annealing=EpisodeAnnealingConfig(enabled=False))
    start, end = day_window()
    add_turn(daemon, 'today', start + 100)
    daemon.store.append_turn_journal('today', 'assistant_exchange', {
        'content': [{'type': 'tool_use', 'id': 'recall-history', 'name': 'recall', 'input': {}}],
        'results': [{'type': 'tool_result', 'tool_use_id': 'recall-history',
                     'content': json.dumps({'ok': True, 'memory': '过去的旧结论不能为今天作证'})}],
    }, trust='runtime')
    with daemon.store._db:
        daemon.store._db.execute('UPDATE turn_journal SET created_at=? WHERE turn_id=?', (start + 100, 'today'))
    daemon.store.claim_due_reflection(ReflectionConfig(enabled=True), end)
    async def reflect(system, messages, tools, turn_id, workflow):
        assert 'recall-history' not in str(messages)
        assert '过去的旧结论不能为今天作证' not in str(messages)
        assert any(t['name'] == 'read_tool_result' for t in tools)
        rejected = await workflow.execute_tool(ToolCall('bad', 'reflection_finish', {
            'summary': '日记', 'memories': [{'kind': 'practice', 'key': 'work.check',
                'content': '认识', 'evidence': '过去的旧结论不能为今天作证', 'confidence': 0.8}],
            'conversation_actions': [],
        }))
        assert rejected['ok'] is False
        accepted = await workflow.execute_tool(ToolCall('done', 'reflection_finish', {
            'summary': '日记', 'memories': [], 'conversation_actions': [],
        }))
        assert accepted['ok'] is True
        return workflow.completion_result()
    daemon._run_agent_workflow = reflect
    asyncio.run(daemon._complete_reflection('2026-09-08', 'reflection-test'))


def test_text_reflection_keeps_speech_and_bounded_execution_evidence(daemon):
    start, end = day_window()
    add_turn(daemon, 'today', start + 100)
    with daemon.store._db:
        daemon.store._db.execute(
            "INSERT INTO messages(turn_id,role,content,created_at,delivery_state,source_event_ids_json) "
            "VALUES ('today','assistant',?,?,'uncertain','[]')",
            ('保留完整正文<&>' + '原话' * 1000, start + 102),
        )
    for name, stamp, result in [
        ('web_fetch', start + 101, {'ok': False, 'error': 'timeout', 'result_ref': 'tr_test',
                                  'content': '很长的工具正文' * 1000}),
        ('recall', start + 101, {'memory': '不重放旧召回'}),
        ('reflection_finish', start + 101, {'summary': '不能用旧复盘自证'}),
        ('web_fetch', end, {'content': '窗口外结果'}),
    ]:
        daemon.store.append_turn_journal('today', 'tool_result', {
            'name': name, 'tool_call_id': name, 'ok': False, 'error': 'timeout', 'result': result,
        }, trust='runtime')
        with daemon.store._db:
            daemon.store._db.execute(
                "UPDATE turn_journal SET created_at=? WHERE turn_id='today' AND sequence="
                "(SELECT MAX(sequence) FROM turn_journal WHERE turn_id='today')", (stamp,),
            )
    rows = daemon.store.conversation_messages_for_turns(None, window=(start, end))
    messages = reflection_transcript(daemon.store, rows, (start, end))
    assert len(messages) == 1 and messages[0]['role'] == 'user'
    text = messages[0]['content']
    assert '## 尚未归类 | ' in text
    assert text.index('USER: today完成项目') < text.index('TOOL CALL: web_fetch') < text.index('ASSISTANT (uncertain):')
    assert 'ASSISTANT (uncertain): 保留完整正文<&>' + '原话' * 1000 in text
    tool = text.split('TOOL CALL: web_fetch', 1)[1].split('\n\nASSISTANT', 1)[0]
    assert len(tool) < 1200
    assert 'timeout' in tool and 'tr_test' in tool
    assert text.count('+08:00') == 1
    assert '不重放旧召回' not in messages[0]['content']
    assert '不能用旧复盘自证' not in messages[0]['content']
    assert '窗口外结果' not in messages[0]['content']


def test_reflection_groups_topics_without_duplicate_or_outside_evidence(daemon):
    start, end = day_window()
    for name, at in [('old', start - 1), ('a1', start + 1), ('b1', start + 2),
                     ('a2', start + 3), ('shared', start + 4), ('loose', start + 5)]:
        add_turn(daemon, name, at)
    for topic, title in [('a', '项目<&>'), ('b', '日常')]:
        daemon.store.create_episode(title, episode_id=topic)
    for topic, turn in [('a', 'old'), ('a', 'a1'), ('b', 'b1'), ('a', 'a2'),
                        ('a', 'shared'), ('b', 'shared')]:
        daemon.store.link_turn_to_episode(topic, turn)
    daemon.store.link_turn_to_episode('b', 'a1', relation='related')
    rows = daemon.store.conversation_messages_for_turns(None, window=(start, end))
    rendered = reflection_transcript(daemon.store, rows, (start, end))[0]['content']
    groups = rendered.split('## ')[1:]
    assert len(groups) == 4
    assert groups[0].startswith('项目<&> | ')
    assert 'USER: a1完成项目\na2完成项目' in groups[0]
    assert groups[1].startswith('日常 | ')
    assert groups[2].startswith('共同材料：项目<&> / 日常 | ')
    assert groups[3].startswith('尚未归类 | ')
    assert rendered.count('+08:00') == 4
    assert '<message' not in rendered and '<topic' not in rendered
    assert sum(rendered.count(row['content']) for row in rows) == len(rows) == 5
    assert rendered.count('shared完成项目') == 1
    assert 'old完成项目' not in rendered


def test_reflection_tool_validation_drops_retry_protocol_and_keeps_failure():
    from momoi.runtime.transcript.reflection import reflection_tool_result

    message = "goal_update: status: 'done' is not one of ['active', 'waiting', 'blocked']."
    text = reflection_tool_result({
        'ok': False, 'error': 'invalid_tool_arguments', 'tool_call_id': 'call_private',
        'result': {'ok': False, 'error': 'invalid_tool_arguments',
                   'message': message + ' Correct arguments only; do not repeat completed actions.',
                   'details': [{'field': 'status'}], 'allowed_fields': ['goal_id', 'status'],
                   'required_fields': ['goal_id'], 'examples': [], 'result_ref': 'tr_validation'},
    }, 'goal_update')
    assert json.loads(text.removeprefix('TOOL RESULT: ')) == {'error': 'invalid_tool_arguments', 'message': message}


def test_reflection_tool_nested_failure_is_not_reported_as_success():
    from momoi.runtime.transcript.reflection import reflection_tool_result

    text = reflection_tool_result({'ok': True, 'result': {
        'ok': False, 'error': 'timeout', 'result': {'message': '远程设备无响应'},
    }}, 'remote_start')
    assert json.loads(text.removeprefix('TOOL RESULT: ')) == {'error': 'timeout', 'message': '远程设备无响应'}
    assert '成功' not in text and '"result"' not in text
    success = reflection_tool_result({'ok': True, 'result': {
        'ok': True, 'result': {'remaining': 6},
    }}, 'inventory')
    assert success == 'TOOL RESULT: {"remaining":6}'


def test_reflection_pairs_call_arguments_by_id_and_preserves_raw_outcome(daemon):
    start, end = day_window()
    add_turn(daemon, 'commands', start + 1)
    for call_id, command in [('first', 'docker ps'), ('second', 'pwd')]:
        daemon.store.append_turn_journal('commands', 'tool_call', {
            'name': 'bash', 'tool_call_id': call_id, 'arguments': {'command': command},
        }, trust='runtime')
    daemon.store.append_turn_journal('commands', 'tool_result', {
        'name': 'bash', 'tool_call_id': 'first', 'ok': True,
        'result': {'exit_code': 0, 'stdout_tail': 'bash: line 1: docker: command not found\n'},
    }, trust='runtime')
    with daemon.store._db:
        daemon.store._db.execute('UPDATE turn_journal SET created_at=? WHERE turn_id=?',
                                 (start + 2, 'commands'))
    text = reflection_transcript(daemon.store, [], (start, end))[0]['content']
    assert 'TOOL CALL: bash("docker ps")\nTOOL RESULT: {"exit_code":0,"stdout_tail":"bash: line 1: docker: command not found\\n"}' in text
    assert 'pwd' not in text and '成功' not in text and '失败' not in text


def test_reflection_excludes_goal_records_and_tools_but_keeps_dialogue(daemon):
    start, end = day_window()
    add_turn(daemon, 'goal-turn', start + 1)
    for name in ['goal_create', 'goal_update', 'goal_finish', 'goal_cancel', 'goal_review']:
        daemon.store.append_turn_journal('goal-turn', 'tool_result', {
            'name': name, 'tool_call_id': name, 'ok': True, 'result': {'content': '内部目标数据'},
        }, trust='runtime')
    with daemon.store._db:
        daemon.store._db.execute('UPDATE turn_journal SET created_at=? WHERE turn_id=?',
                                 (start + 2, 'goal-turn'))
    rows = daemon.store.conversation_messages_for_turns(None, window=(start, end))
    rows.append({**rows[0], 'role': 'goal', 'content': '内部目标快照'})
    text = reflection_transcript(daemon.store, rows, (start, end))[0]['content']
    assert 'USER: goal-turn完成项目' in text
    assert '内部目标' not in text and 'GOAL:' not in text and 'TOOL CALL:' not in text


def test_reflection_excludes_internal_heartbeat_but_keeps_delivered_speech(daemon):
    start, end = day_window()
    add_turn(daemon, 'heartbeat-turn', start + 1)
    rows = daemon.store.conversation_messages_for_turns(None, window=(start, end))
    rows = [
        {**rows[0], 'role': 'heartbeat', 'content': '内部心跳状态'},
        {**rows[0], 'role': 'assistant', 'content': '记得喝水', 'delivery_state': 'delivered'},
    ]
    text = reflection_transcript(daemon.store, rows, (start, end))[0]['content']
    assert '内部心跳状态' not in text and 'HEARTBEAT:' not in text
    assert 'ASSISTANT: 记得喝水' in text


@pytest.mark.parametrize('archive_kind', ['webhook', 'goal', 'heartbeat'])
def test_reflection_excludes_whole_runtime_topic_and_summary(daemon, archive_kind):
    start, end = day_window()
    add_turn(daemon, 'archive-turn', start + 1)
    add_turn(daemon, 'owner-turn', start + 2)
    daemon.store.create_episode('排除的归档话题', episode_id='archive')
    daemon.store.link_turn_to_episode('archive', 'archive-turn')
    daemon.store.append_turn_journal('archive-turn', 'tool_result', {
        'name': 'bash', 'tool_call_id': 'archive-call', 'ok': True,
        'result': {'stdout': '归档工具结果'},
    }, trust='runtime')
    with daemon.store._db:
        daemon.store._db.execute('UPDATE conversation_episodes SET archive_kind=? WHERE id=?',
                                 (archive_kind, 'archive'))
        daemon.store._db.execute('UPDATE turn_journal SET created_at=? WHERE turn_id=?',
                                 (start + 3, 'archive-turn'))
    rows = daemon.store.conversation_messages_for_turns(None, window=(start, end))
    text = reflection_transcript(daemon.store, rows, (start, end))[0]['content']
    assert '排除的归档话题' not in text and '归档工具结果' not in text
    assert 'archive-turn完成项目' not in text
    assert 'owner-turn完成项目' in text
    source = daemon.store.reflection_source('2026-09-08')
    assert '排除的归档话题' not in source['episode_timeline']
