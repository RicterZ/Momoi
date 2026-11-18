import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from momoi.models import AgentReply, ProviderResponse, ToolCall
from momoi.runtime import MomoiDaemon
from momoi.runtime.workflows import subagent as subagent_workflow
from momoi.storage import Store
from momoi.storage.core.migrations import MIGRATIONS, _add_subagent_notifications, _replace_plans_with_subagents
from tests.support import config


def test_queue_runs_one_task_at_a_time_and_cancel_is_final(tmp_path):
    store = Store(tmp_path / "db")
    try:
        first = store.create_subagent_task("turn", "onebot", "第一", "任务一")
        assert store.create_subagent_task("turn", "onebot", "第一", "任务一")["id"] == first["id"]
        second = store.create_subagent_task("turn", "onebot", "第二", "任务二")
        assert (first["queue_position"], second["queue_position"]) == (0, 1)
        assert store.claim_subagent_task()["id"] == first["id"]
        assert store.claim_subagent_task() is None
        store.add_subagent_report(first["id"], "进行中")
        with pytest.raises(ValueError):
            store.add_subagent_report(second["id"], "还没开始")
        assert store.cancel_subagent_task(second["id"], "onebot")["status"] == "cancelled"
        with pytest.raises(ValueError):
            store.cancel_subagent_task(first["id"], "other")
        done = store.finish_subagent_task(first["id"], "succeeded", "完成", [])
        assert done["status"] == "succeeded" and done["reports"][0]["progress"] == "进行中"
        assert store.finish_subagent_task(first["id"], "failed", "重复", []) is None
        assert store.claim_subagent_task() is None
    finally:
        store.close()


def test_restart_fails_running_task_and_keeps_queue(tmp_path):
    store = Store(tmp_path / "db")
    running = store.create_subagent_task("turn", "onebot", "运行中", "任务")
    queued = store.create_subagent_task("turn", "onebot", "排队", "任务")
    store.claim_subagent_task()
    store.close()
    store = Store(tmp_path / "db")
    try:
        store.recover_subagent_tasks()
        assert store.subagent_task(running["id"])["status"] == "failed"
        assert store.claim_subagent_task()["id"] == queued["id"]
    finally:
        store.close()


def test_migration_cancels_active_plans_and_creates_subagent_table(tmp_path):
    store = Store(tmp_path / "db")
    with store._db:
        store._db.execute("DROP TABLE subagent_tasks")
        store._db.execute(
            "INSERT INTO task_plans (id, source_turn_id, channel, title, request, status, steps_json, created_at, updated_at) "
            "VALUES ('p', 'turn', 'onebot', 't', 'r', 'running', '[]', 0, 0)"
        )
        store._db.execute(f"PRAGMA user_version={MIGRATIONS.index(_replace_plans_with_subagents)}")
    store.close()
    store = Store(tmp_path / "db")
    try:
        assert store._db.execute("SELECT status FROM task_plans WHERE id='p'").fetchone()[0] == "cancelled"
        assert store.create_subagent_task("turn", "onebot", "新", "任务")["status"] == "queued"
    finally:
        store.close()


def test_worker_failure_reports_latest_progress_to_planner(tmp_path):
    async def run():
        daemon = MomoiDaemon(config(str(tmp_path)))
        daemon._request_webhook_turn = AsyncMock(return_value=AgentReply([]))
        calls = 0

        async def complete(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return ProviderResponse([], [ToolCall("r", "subagent_report", {"progress": "已找到两个候选"})])
            raise RuntimeError("provider down")

        daemon.provider = type("P", (), {"complete": staticmethod(complete)})()
        daemon.store.create_subagent_task("missing-turn", next(iter(daemon.channels)), "查资料", "查一下")
        task = daemon.store.claim_subagent_task()
        await daemon._run_subagent_task(task)
        await asyncio.gather(*daemon._subagent_wakeups)
        finished = daemon.store.subagent_task(task["id"])
        assert finished["status"] == "failed" and "已找到两个候选" in finished["summary"]
        calls = daemon._request_webhook_turn.await_args_list
        assert len(calls) == 1 and "已结束" in calls[0].args[0]
        assert calls[0].kwargs["parent_turn_id"] == finished["turn_id"]
        assert finished["notified_at"] is not None
        daemon.store.close()
    asyncio.run(run())


def test_planner_wake_turn_is_committed(tmp_path):
    async def run():
        daemon = MomoiDaemon(config(str(tmp_path)))
        channel = next(iter(daemon.channels))

        async def wake(_prompt, turn_id, **kwargs):
            daemon.store.begin_turn(turn_id, "webhook", [turn_id], parent_turn_id=kwargs["parent_turn_id"])
            daemon.store.queue_progress(turn_id, "reply-1", ["关好啦"], channel)
            return AgentReply(["灯关好了"])

        daemon._request_webhook_turn = wake

        async def complete(*_args, **_kwargs):
            return ProviderResponse([], [ToolCall("f", "subagent_finish", {"status": "succeeded", "summary": "已关灯"})])

        daemon.provider = type("P", (), {"complete": staticmethod(complete)})()
        daemon.store.create_subagent_task("turn", channel, "关灯", "关客厅灯")
        await daemon._run_subagent_task(daemon.store.claim_subagent_task())
        await asyncio.gather(*daemon._subagent_wakeups)
        rows = daemon.store._db.execute(
            "SELECT state FROM turns WHERE workflow_kind='webhook'").fetchall()
        assert [row[0] for row in rows] == ["completed"]
        assert daemon.store._db.execute(
            "SELECT target_channel FROM outbox WHERE text='灯关好了'").fetchone()[0] == channel
        assert [row[0] for row in daemon.store._db.execute(
            "SELECT m.content FROM messages m JOIN turns t ON t.id=m.turn_id "
            "WHERE t.workflow_kind='webhook' AND m.role='assistant' ORDER BY m.id")] == ["关好啦", "灯关好了"]
        daemon.store.close()
    asyncio.run(run())


def test_planner_wake_turn_releases_dispatcher(tmp_path):
    async def run():
        daemon = MomoiDaemon(config(str(tmp_path)))

        async def dispatcher():
            _prompt, turn_id, future = await daemon.webhook_requests.get()
            committed = daemon._webhook_commits[turn_id]
            daemon.store.begin_turn(turn_id, "webhook", [turn_id])
            future.set_result(AgentReply([]))
            await asyncio.wait_for(committed, 2)

        async def complete(*_args, **_kwargs):
            return ProviderResponse([], [ToolCall("f", "subagent_finish", {"outcome": "succeeded", "summary": "好了", "output_refs": []})])

        daemon.provider = type("P", (), {"complete": staticmethod(complete)})()
        consumer = asyncio.create_task(dispatcher())
        daemon.store.create_subagent_task("turn", next(iter(daemon.channels)), "关灯", "关客厅灯")
        await daemon._run_subagent_task(daemon.store.claim_subagent_task())
        await asyncio.gather(*daemon._subagent_wakeups)
        await consumer
        assert not daemon._webhook_commits
        daemon.store.close()
    asyncio.run(run())


def test_progress_wake_fires_while_worker_keeps_running(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent_workflow, "SUBAGENT_PROGRESS_WAKE_DELAY_SECONDS", 0)

    async def run():
        daemon = MomoiDaemon(config(str(tmp_path)))
        daemon._request_webhook_turn = AsyncMock()
        calls = 0

        async def complete(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return ProviderResponse([], [ToolCall("r", "subagent_report", {"progress": "过半"})])
            await asyncio.sleep(0.05)
            return ProviderResponse([], [ToolCall("f", "subagent_finish", {
                "outcome": "succeeded", "summary": "好了", "output_refs": []})])

        daemon.provider = type("P", (), {"complete": staticmethod(complete)})()
        daemon.store.create_subagent_task("turn", next(iter(daemon.channels)), "查资料", "查一下")
        await daemon._run_subagent_task(daemon.store.claim_subagent_task())
        await asyncio.gather(*daemon._subagent_wakeups)
        prompts = [call.args[0] for call in daemon._request_webhook_turn.await_args_list]
        assert len(prompts) == 2 and "过半" in prompts[0] and "已结束" in prompts[1]
        daemon.store.close()
    asyncio.run(run())


def test_unnotified_result_is_resent_after_restart(tmp_path):
    store = Store(tmp_path / "momoi.sqlite3")
    channel = "onebot"
    interrupted = store.create_subagent_task("turn", channel, "中断", "任务")
    store.claim_subagent_task()
    store.close()

    async def run():
        daemon = MomoiDaemon(config(str(tmp_path)))
        daemon._request_webhook_turn = AsyncMock(return_value=AgentReply([]))
        daemon.store.commit_subagent_report_reply = lambda *_args: []
        assert daemon.store.subagent_task(interrupted["id"])["status"] == "failed"
        daemon._resend_subagent_wakeups()
        await asyncio.gather(*daemon._subagent_wakeups)
        assert "运行时重启" in daemon._request_webhook_turn.await_args.args[0]
        assert daemon.store.subagent_task(interrupted["id"])["notified_at"] is not None
        daemon._request_webhook_turn.reset_mock()
        daemon._resend_subagent_wakeups()
        daemon._request_webhook_turn.assert_not_awaited()
        daemon.store.close()
    asyncio.run(run())


def test_resend_skips_wake_turn_that_already_completed(tmp_path):
    async def run():
        daemon = MomoiDaemon(config(str(tmp_path)))
        daemon._request_webhook_turn = AsyncMock()
        task = daemon.store.create_subagent_task("turn", "onebot", "关灯", "关灯")
        daemon.store.claim_subagent_task()
        task = daemon.store.finish_subagent_task(task["id"], "succeeded", "好了", [])
        wake_turn = daemon._turn_id("subagent-report", task["id"], "finished", 0)
        daemon.store.begin_turn(wake_turn, "webhook", [wake_turn])
        daemon.store.commit_subagent_report_reply(wake_turn, AgentReply([]), "onebot")
        daemon._resend_subagent_wakeups()
        daemon._request_webhook_turn.assert_not_awaited()
        assert daemon.store.subagent_task(task["id"])["notified_at"] is not None
        daemon.store.close()
    asyncio.run(run())


def test_event_turn_reply_commits_staged_goals(tmp_path):
    store = Store(tmp_path / "db")
    try:
        goal = {
            "id": "g1", "title": "核实门口监控", "success_criteria": "核实清楚",
            "source_event_id": "webhook-turn", "status": "active",
            "next_action": "等 Worker 结果", "waiting_for": "", "blocked_reason": "",
            "latest_result": "", "schedule": None, "next_review_at": time.time() + 3600,
        }
        store.begin_turn("webhook-turn", "webhook", ["webhook-turn"])
        store.commit_subagent_report_reply("webhook-turn", AgentReply([], goals={"g1": goal}), "onebot")
        assert store.goal("g1")["status"] == "active"
        store.begin_turn("report-turn", "subagent_report", ["report-turn"])
        closed = {**goal, "status": "done", "latest_result": "保洁拖地", "next_review_at": None}
        store.commit_subagent_report_reply("report-turn", AgentReply([], goals={"g1": closed}), "onebot")
        assert store.goal("g1")["status"] == "done"
    finally:
        store.close()


def test_migration_marks_existing_results_notified(tmp_path):
    store = Store(tmp_path / "db")
    task = store.create_subagent_task("turn", "onebot", "旧", "任务")
    store.claim_subagent_task()
    store.finish_subagent_task(task["id"], "succeeded", "好了", [])
    with store._db:
        store._db.execute("UPDATE subagent_tasks SET notified_at=NULL")
        store._db.execute(f"PRAGMA user_version={MIGRATIONS.index(_add_subagent_notifications)}")
    store.close()
    store = Store(tmp_path / "db")
    try:
        assert store.unnotified_subagent_tasks(max_age_seconds=3600) == []
    finally:
        store.close()


def test_voice_call_result_falls_back_to_text_channel(tmp_path):
    daemon = MomoiDaemon(config(str(tmp_path)))
    try:
        class Call:
            dialogue_channel = "onebot"
            active = True
            def routing_context(self):
                return {}
            def session_valid(self, _context):
                return self.active

        call = Call()
        daemon.channels = {"onebot": object(), "qq_call": call}
        assert daemon._subagent_reply_channel("qq_call") == "qq_call"
        call.active = False
        assert daemon._subagent_reply_channel("qq_call") == "onebot"
        assert daemon._subagent_reply_channel("onebot") == "onebot"
    finally:
        daemon.store.close()
