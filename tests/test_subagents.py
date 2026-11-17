import asyncio
from unittest.mock import AsyncMock

import pytest

from momoi.models import AgentReply, ProviderResponse, ToolCall
from momoi.runtime import MomoiDaemon
from momoi.storage import Store
from momoi.storage.core.migrations import SCHEMA_VERSION
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
        store._db.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")
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
        daemon._request_webhook_turn = AsyncMock()
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
        kinds = [call.args[1] for call in daemon._request_webhook_turn.await_args_list]
        assert len(kinds) == 2 and kinds[0] != kinds[1]
        assert all(call.kwargs["parent_turn_id"] == finished["turn_id"]
                   for call in daemon._request_webhook_turn.await_args_list)
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
