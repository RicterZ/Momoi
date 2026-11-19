import asyncio
import json
import time

from momoi.models import ToolCall
from momoi.runtime.workflows.worker_review import (
    admission_reasons, parse_worker_review, recurring_failures, redact, render_task, worker_review_input,
)
from momoi.storage import Store

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJhYmNkZWZnaCJ9.c2lnbmF0dXJlc2lnbmF0dXJl"


def add_task(store, title, steps, *, status="succeeded", task="做一件事", duration=10, finished_at=None):
    task_id = store.create_subagent_task(f"source-{title}", "qq", title, task)["id"]
    turn_id = f"worker-{title}"
    store.begin_turn(turn_id, "subagent", [f"subagent:{task_id}"])
    store.attach_subagent_turn(task_id, turn_id)
    for index, (name, ok, error, result) in enumerate(steps):
        call_id = f"call-{index}"
        store.append_turn_journal(turn_id, "tool_call", {"tool_call_id": call_id, "name": name, "arguments": {"q": index}})
        store.append_turn_journal(turn_id, "tool_result", {"tool_call_id": call_id, "name": name, "ok": ok,
                                                          "error": error, "result": result or {"ok": ok}})
    store.append_turn_journal(turn_id, "tool_call", {"tool_call_id": "finish", "name": "subagent_finish", "arguments": {}})
    finished = finished_at or time.time()
    store._db.execute("UPDATE subagent_tasks SET status=?, summary='完成', started_at=?, finished_at=? WHERE id=?",
                      (status, finished - duration, finished, task_id))
    store._db.commit()
    return task_id


def ok(name="exec"):
    return (name, True, None, None)


def fail(name="exec", error="tool_failed", result=None):
    return (name, False, error, result)


def test_admission_signals(tmp_path):
    store = Store(tmp_path / "db")
    simple = add_task(store, "simple", [ok(), ok(), fail()])
    failed = add_task(store, "failed", [ok()], status="failed")
    failures = add_task(store, "failures", [fail(), ok(), fail("web_fetch", "http_error")])
    many = add_task(store, "many", [ok("weibo"), ok("exec")] * 8)
    slow = add_task(store, "slow", [ok()], duration=300)
    run = add_task(store, "run", [ok("exec")] * 8)
    brave_a = add_task(store, "brave-a", [fail("brave", "mcp_tool_error"), ok("brave")])
    brave_b = add_task(store, "brave-b", [ok("web_fetch"), fail("brave", "mcp_tool_error")])
    cancelled = add_task(store, "cancelled", [ok()], status="cancelled")
    batch = [(task, store.worker_task_steps(task["turn_id"]))
             for task in store.unreviewed_worker_tasks(start_at=0, end_at=time.time() + 1)]
    recurring = recurring_failures(batch)
    reasons = {task["id"]: admission_reasons(task, steps, recurring) for task, steps in batch}
    assert reasons[simple] == [] and reasons[cancelled] == []
    assert reasons[failed] == ["not_succeeded"]
    assert reasons[failures] == ["tool_failures"]
    assert reasons[many] == ["many_calls"]
    assert reasons[slow] == ["long_running"]
    assert reasons[run] == ["same_tool_run"]
    assert reasons[brave_a] == reasons[brave_b] == ["recurring_failure"]
    assert recurring == {("brave", "mcp_tool_error"): [brave_a, brave_b]}
    assert all(step["name"] != "subagent_finish" for _, steps in batch for step in steps)
    store.close()


def test_material_redacts_and_reads_old_mcp_failures(tmp_path):
    store = Store(tmp_path / "db")
    task_id = add_task(store, "brave", [fail("mcp__brave__search", "mcp_tool_error", {"ok": False, "result_ref": "tr_1"}),
                                        ok("mcp__brave__search")],
                       task=f"Authorization: Bearer {JWT} 然后 <查询>", status="failed")
    task = store.subagent_task(task_id)
    steps = store.worker_task_steps(task["turn_id"])
    snapshots = {"tr_1": json.dumps({"message": "429 Too Many Requests"})}
    content = worker_review_input([render_task(task, steps, ["not_succeeded"], snapshots.get)], {})
    assert JWT not in content and "&lt;redacted&gt;" in content
    assert "&lt;查询&gt;" in content and "429 Too Many Requests" in content
    assert f'id="{task_id}"' in content and 'admitted_by="not_succeeded"' in content
    assert "[2] mcp__brave__search ok" in content and "review_period" not in content
    assert redact("token=abcdefghijklmnop123") == "token=<redacted>"
    store.close()


def test_finish_requires_a_verdict_per_task_and_valid_evidence():
    admitted = [({"id": "t1"}, [{}, {}], ["many_calls"]), ({"id": "t2"}, [{}], ["long_running"])]
    item = {"problem": "第 1 步没有排序。", "kind": "execution", "applies_to": "在 B 站找当天新投稿", "content": "按发布时间排序。",
            "pitfalls": [], "evidence": [{"task_id": "t1", "steps": [2]}]}
    tasks = [{"task_id": "t1", "verdict": "learn", "reason": "找到排序方法"},
             {"task_id": "t2", "verdict": "skip", "reason": "每一步都必要"}]
    assert parse_worker_review({"summary": "s", "tasks": tasks, "experiences": [item]}, admitted)[1] is None
    skipped = [{**task, "verdict": "skip"} for task in tasks]
    assert parse_worker_review({"summary": "s", "tasks": skipped, "experiences": []}, admitted)[1] is None
    assert parse_worker_review({"summary": "s", "tasks": tasks, "experiences": []}, admitted)[1]["error"] == "learn_without_experience"
    assert parse_worker_review({"summary": "s", "tasks": tasks[:1], "experiences": [item]}, admitted)[1]["error"] == "tasks_mismatch"
    assert parse_worker_review({"summary": "s", "tasks": [*tasks, tasks[1]], "experiences": [item]}, admitted)[1]["error"] == "tasks_mismatch"
    assert parse_worker_review({"summary": "s", "tasks": skipped, "experiences": [item] * 4}, admitted)[1]
    for bad in ({"evidence": [{"task_id": "t9", "steps": []}]}, {"evidence": [{"task_id": "t1", "steps": [3]}]},
                {"kind": "memory"}, {"content": " "}):
        assert parse_worker_review({"summary": "s", "tasks": tasks, "experiences": [{**item, **bad}]}, admitted)[1]


def test_review_runs_after_reflection_once_and_skips_model_without_admission(daemon):
    store = daemon.store
    simple = add_task(store, "simple", [ok()])
    window = time.time() + 1
    calls = []

    async def no_model(*args, **kwargs):
        calls.append(args)

    daemon._run_agent_workflow = no_model
    asyncio.run(daemon._complete_worker_review("2026-11-18", window))
    assert not calls
    assert store._db.execute("SELECT admitted FROM worker_reviews WHERE task_id=?", (simple,)).fetchone()[0] == 0

    failed = add_task(store, "failed", [fail(), fail()], status="failed")

    async def finish(system, messages, tools, *, turn_id, workflow):
        assert [tool["name"] for tool in tools] == ["worker_review_finish"]
        assert "Worker 执行复盘" in system and "companion soul sentinel" not in str(system)
        assert f'id="{failed}"' in messages[0]["content"] and simple not in messages[0]["content"]
        bad = await workflow.execute_tool(ToolCall("f", "worker_review_finish", {
            "summary": "s", "tasks": [{"task_id": failed, "verdict": "skip", "reason": "r"}], "experiences": [{"problem": "p", "kind": "dispatch", "applies_to": "a", "content": "c",
                                              "pitfalls": [], "evidence": [{"task_id": simple, "steps": []}]}]}))
        assert bad["error"] == "unknown_task"
        result = await workflow.execute_tool(ToolCall("f", "worker_review_finish", {
            "summary": "一个失败任务", "tasks": [{"task_id": failed, "verdict": "learn", "reason": "命令不存在"}], "experiences": [{"problem": "第 1 步命令不存在。", "kind": "execution", "applies_to": "执行命令", "content": "先检查命令是否存在。",
                                                   "pitfalls": ["别重试"], "evidence": [{"task_id": failed, "steps": [1, 2]}]}]}))
        assert result["ok"]
        return workflow.completion_result()

    daemon._run_agent_workflow = finish
    asyncio.run(daemon._complete_worker_review("2026-11-19", window))
    [candidate] = store.worker_experience_candidates()
    assert candidate["kind"] == "execution" and candidate["evidence"] == [{"task_id": failed, "steps": [1, 2]}]
    assert store.unreviewed_worker_tasks(start_at=0, end_at=time.time() + 10) == []
    assert store._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_material_budget_defers_later_tasks(daemon, monkeypatch):
    from momoi.runtime.workflows import worker_review
    store = daemon.store
    first = add_task(store, "first", [ok()], status="failed", finished_at=time.time() - 30)
    second = add_task(store, "second", [ok()], status="failed", finished_at=time.time() - 20)
    monkeypatch.setattr(worker_review, "MATERIAL_CHARS", 10)
    seen = []

    async def finish(system, messages, tools, *, turn_id, workflow):
        seen.append(messages[0]["content"])
        await workflow.execute_tool(ToolCall("f", "worker_review_finish", {
            "summary": "s", "tasks": [{"task_id": first, "verdict": "skip", "reason": "r"}], "experiences": []}))
        return workflow.completion_result()

    daemon._run_agent_workflow = finish
    asyncio.run(daemon._complete_worker_review("2026-11-18", time.time() + 1))
    assert first in seen[0] and second not in seen[0]
    assert [task["id"] for task in store.unreviewed_worker_tasks(start_at=0, end_at=time.time() + 10)] == [second]


def test_journal_keeps_mcp_failure_message_only(daemon):
    from momoi.models import TurnDraft
    executor = daemon.tool_executor
    daemon.store.begin_turn("worker-turn", "subagent", ["subagent:x"])
    for index, result in enumerate(({"ok": False, "error": "mcp_tool_error", "message": "429 Too Many Requests",
                                     "result_ref": "tr_a", "result": {"content": "large"}},
                                    {"ok": True, "error": None, "message": "hello", "result_ref": "tr_b"})):
        call = ToolCall(f"c{index}", "mcp__brave__search", {})
        trace = executor.begin_trace(call, "mcp", turn_id="worker-turn", stage="subagent",
                                     call_id="model", round_number=1, channel="qq")
        executor.finish_trace(trace, call, result, TurnDraft())
    failed, succeeded = daemon.store.worker_task_steps("worker-turn")
    assert failed["result"] == {"ok": False, "error": "mcp_tool_error", "message": "429 Too Many Requests",
                                "result_ref": "tr_a"}
    assert succeeded["result"] == {"ok": True, "error": None, "result_ref": "tr_b"}


def test_failed_review_leaves_tasks_for_next_run(daemon):
    store = daemon.store
    failed = add_task(store, "failed", [ok()], status="failed")

    async def boom(*args, **kwargs):
        raise RuntimeError("provider failed")

    daemon._run_agent_workflow = boom
    asyncio.run(daemon._complete_worker_review("2026-11-18", time.time() + 1))
    assert [task["id"] for task in store.unreviewed_worker_tasks(start_at=0, end_at=time.time() + 10)] == [failed]
