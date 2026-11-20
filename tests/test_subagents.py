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
        assert len(calls) == 2 and "已找到两个候选" in calls[0].args[0] and "已结束" in calls[1].args[0]
        assert calls[1].kwargs["parent_turn_id"] == finished["turn_id"]
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


def test_every_progress_report_wakes_planner_immediately(tmp_path):

    async def run():
        daemon = MomoiDaemon(config(str(tmp_path)))
        daemon._request_webhook_turn = AsyncMock(return_value=AgentReply([]))
        calls = 0

        async def complete(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return ProviderResponse([], [ToolCall("r", "subagent_report", {"progress": "过半"})])
            if calls == 2:
                return ProviderResponse([], [ToolCall("q", "subagent_report", {"progress": "请扫码"})])
            await asyncio.sleep(0.05)
            return ProviderResponse([], [ToolCall("f", "subagent_finish", {
                "outcome": "succeeded", "summary": "好了", "output_refs": []})])

        daemon.provider = type("P", (), {"complete": staticmethod(complete)})()
        task_id = daemon.store.create_subagent_task("turn", next(iter(daemon.channels)), "查资料", "查一下")["id"]
        await daemon._run_subagent_task(daemon.store.claim_subagent_task())
        await asyncio.gather(*daemon._subagent_wakeups)
        prompts = [call.args[0] for call in daemon._request_webhook_turn.await_args_list]
        assert len(prompts) == 3 and "过半" in prompts[0] and "请扫码" in prompts[1] and "已结束" in prompts[2]
        assert daemon.store.subagent_task(task_id)["notified_at"] is not None
        daemon.store.close()
    asyncio.run(run())


def test_planner_message_is_injected_and_reply_wakes_planner(tmp_path):
    async def run():
        daemon = MomoiDaemon(config(str(tmp_path)))
        daemon._request_webhook_turn = AsyncMock(return_value=AgentReply([]))
        channel = next(iter(daemon.channels))
        task_id = daemon.store.create_subagent_task("turn", channel, "查电量", "查扫地机电量")["id"]
        seen: list[str] = []
        message_id = ""

        async def complete(_system, messages, *_args, **_kwargs):
            nonlocal message_id
            seen.append(str(messages[-1]["content"]))
            if len(seen) == 1:
                message_id = daemon.message_running_subagent(task_id, "现在多少电量？")["message_id"]
                return ProviderResponse([], [ToolCall("e", "subagent_report", {"progress": "开始查询"})])
            if len(seen) == 2:
                return ProviderResponse([], [ToolCall("r", "subagent_report", {
                    "progress": "84%", "reply_to": message_id})])
            return ProviderResponse([], [ToolCall("f", "subagent_finish", {
                "outcome": "succeeded", "summary": "已启动", "output_refs": []})])

        daemon.provider = type("P", (), {"complete": staticmethod(complete)})()
        await daemon._run_subagent_task(daemon.store.claim_subagent_task())
        await asyncio.gather(*daemon._subagent_wakeups)
        assert f'<planner_message id="{message_id}">' in seen[1] and "现在多少电量？" in seen[1]
        prompts = [call.args[0] for call in daemon._request_webhook_turn.await_args_list]
        assert any("回答了你的消息" in p and "现在多少电量？" in p and "84%" in p for p in prompts)
        assert "已结束" in prompts[-1]
        assert daemon.message_running_subagent(task_id, "还在吗")["error"] == "task_not_running"
        daemon.store.close()
    asyncio.run(run())


def test_planner_messages_are_rate_limited_and_unread_ones_reported(tmp_path):
    async def run():
        daemon = MomoiDaemon(config(str(tmp_path)))
        daemon._request_webhook_turn = AsyncMock(return_value=AgentReply([]))
        task_id = daemon.store.create_subagent_task("turn", next(iter(daemon.channels)), "查", "查一下")["id"]
        results = []

        async def complete(*_args, **_kwargs):
            results.extend(daemon.message_running_subagent(task_id, f"问题{i}") for i in range(6))
            return ProviderResponse([], [ToolCall("f", "subagent_finish", {
                "outcome": "succeeded", "summary": "好了", "output_refs": []})])

        daemon.provider = type("P", (), {"complete": staticmethod(complete)})()
        await daemon._run_subagent_task(daemon.store.claim_subagent_task())
        await asyncio.gather(*daemon._subagent_wakeups)
        assert [r["ok"] for r in results] == [True] * 5 + [False]
        assert results[-1]["error"] == "subagent_message_rate_limited"
        prompt = daemon._request_webhook_turn.await_args_list[-1].args[0]
        assert "没有读到你发的消息" in prompt and "问题4" in prompt
        daemon.store.close()
    asyncio.run(run())


def test_worker_block_suspends_until_planner_message(tmp_path):
    async def run():
        daemon = MomoiDaemon(config(str(tmp_path)))
        task_id = daemon.store.create_subagent_task("turn", next(iter(daemon.channels)), "开空调", "开空调")["id"]

        async def wake(prompt, *_args, **_kwargs):
            if "开哪层" in prompt:
                await asyncio.sleep(0.05)
                daemon.message_running_subagent(task_id, "二楼")
            return AgentReply([])

        daemon._request_webhook_turn = wake
        seen = []

        async def complete(_system, messages, *_args, **_kwargs):
            seen.append(messages[-1]["content"])
            if len(seen) == 1:
                return ProviderResponse([], [ToolCall("b", "subagent_block", {"reason": "开哪层？"})])
            return ProviderResponse([], [ToolCall("f", "subagent_finish", {
                "outcome": "succeeded", "summary": "已开二楼", "output_refs": []})])

        daemon.provider = type("P", (), {"complete": staticmethod(complete)})()
        await asyncio.wait_for(daemon._run_subagent_task(daemon.store.claim_subagent_task()), 5)
        await asyncio.gather(*daemon._subagent_wakeups)
        assert len(seen) == 2 and "<planner_message" in str(seen[1]) and "二楼" in str(seen[1])
        assert daemon.store.subagent_task(task_id)["status"] == "succeeded"
        daemon.store.close()
    asyncio.run(run())


def test_worker_block_times_out_as_blocked(tmp_path, monkeypatch):
    real_wait_for = asyncio.wait_for

    async def short_wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, min(timeout, 0.01))

    monkeypatch.setattr(subagent_workflow.asyncio, "wait_for", short_wait_for)

    async def run():
        daemon = MomoiDaemon(config(str(tmp_path)))
        daemon._request_webhook_turn = AsyncMock(return_value=AgentReply([]))
        task_id = daemon.store.create_subagent_task("turn", next(iter(daemon.channels)), "开空调", "开空调")["id"]
        calls = 0

        async def complete(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return ProviderResponse([], [ToolCall("b", "subagent_block", {"reason": "开哪层？", "timeout_seconds": 30})])

        daemon.provider = type("P", (), {"complete": staticmethod(complete)})()
        await daemon._run_subagent_task(daemon.store.claim_subagent_task())
        await asyncio.gather(*daemon._subagent_wakeups)
        task = daemon.store.subagent_task(task_id)
        assert calls == 1 and task["status"] == "blocked" and "开哪层？" in task["summary"]
        prompts = [call.args[0] for call in daemon._request_webhook_turn.await_args_list]
        assert "已挂起等你回复" in prompts[0] and "已结束" in prompts[-1]
        daemon.store.close()
    asyncio.run(run())


def test_replayed_turn_sees_earlier_workers_before_dispatching(tmp_path):
    async def run():
        daemon = MomoiDaemon(config(str(tmp_path)))
        channel = next(iter(daemon.channels))
        earlier = daemon.store.create_subagent_task("turn-1", channel, "开空调制热", "开二楼空调")
        daemon.store.claim_subagent_task()
        daemon.store.finish_subagent_task(earlier["id"], "failed", "Worker 被中断，可能已有部分操作生效。", [])
        request = type("R", (), {"turn_id": "turn-1",
                                 "delivery_channel": type("C", (), {"name": channel})()})()
        call = ToolCall("c", "create_subagent", {"title": "开空调并核实", "task": "开二楼空调"})
        first = daemon.tool_batch._subagent_tool(call, request)
        assert first["error"] == "turn_has_earlier_subagent_tasks"
        assert first["earlier_tasks"][0]["task_id"] == earlier["id"]
        second = daemon.tool_batch._subagent_tool(call, request)
        assert second["ok"] and second["task_id"] != earlier["id"]
        third = daemon.tool_batch._subagent_tool(
            ToolCall("d", "create_subagent", {"title": "另一件事", "task": "查天气"}), request)
        assert third["ok"]
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


def test_worker_task_carries_triggered_worker_practices_only(tmp_path, monkeypatch):
    import json
    from momoi.models import IncomingMessage
    from momoi.runtime.tool_contracts.context import recall_search_arguments
    from tests.support import seed_memory

    async def run():
        daemon = MomoiDaemon(config(str(tmp_path)))
        daemon._request_webhook_turn = AsyncMock(return_value=AgentReply([]))
        event = IncomingMessage("e", "owner", "x", time.time(), time.time())
        for key, scope, content in (("practice.search.rate-limit", "worker", "两次搜索之间间隔几秒"),
                                    ("practice.search.global", "", "全局记忆不该注入")):
            memory_id = seed_memory(daemon.store, event, key=key, content=content, kind="practice",
                                    activation="scoped" if scope else "recall")
            with daemon.store._db:
                daemon.store._db.execute("UPDATE memories SET scope_key=?, meta_json=? WHERE id=?", (scope, json.dumps(
                    {"scope": scope, "triggers": ["搜索"], "verification": "verified", "pitfalls": ["429 后先等"]},
                    ensure_ascii=False), memory_id))
        seen = []

        async def complete(_system, messages, *_args, **_kwargs):
            seen.append(messages[0]["content"])
            return ProviderResponse([], [ToolCall("f", "subagent_finish", {"outcome": "succeeded", "summary": "完成", "output_refs": []})])

        daemon.provider = type("P", (), {"complete": staticmethod(complete)})()
        channel = next(iter(daemon.channels))
        daemon.store.create_subagent_task("turn", channel, "影子", "搜索一下店铺")
        await daemon._run_subagent_task(daemon.store.claim_subagent_task())
        assert "<practices>" not in seen.pop()
        monkeypatch.setattr(subagent_workflow, "WORKER_PRACTICE_INJECT", True)
        for title, text in (("查资料", "搜索一下店铺"), ("关灯", "关客厅灯")):
            daemon.store.create_subagent_task("turn", channel, title, text)
            await daemon._run_subagent_task(daemon.store.claim_subagent_task())
        assert "<practices>" in seen[0] and "两次搜索之间间隔几秒" in seen[0] and "<pitfall>429 后先等</pitfall>" in seen[0]
        assert "全局记忆不该注入" not in seen[0] and "<practices>" not in seen[1]
        with pytest.raises(ValueError):
            recall_search_arguments({"scope": "worker"})
        assert daemon.store.memories.triggered("搜索", scope="")[0]["key"] == "practice.search.global"
        daemon.store.close()
    asyncio.run(run())
