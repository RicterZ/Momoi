"""Finite, immediate plans, independent of scheduled goals."""
import json
import time
import uuid

PLAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_plans (
 id TEXT PRIMARY KEY, source_turn_id TEXT NOT NULL, channel TEXT NOT NULL,
 title TEXT NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL,
 steps_json TEXT NOT NULL, step_index INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 UNIQUE(source_turn_id, title)
);
"""


class PlanStore:
    def task_plan(self, plan_id):
        row = self._db.execute("SELECT * FROM task_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            return None
        plan = dict(row)
        plan["steps"] = json.loads(plan.pop("steps_json"))
        plan["version"] = int(plan.get("version", 1))
        plan["context"] = json.loads(plan.pop("context_json") or "null")
        return plan

    def create_task_plan(self, args, turn_id, channel):
        if set(args) != {"title", "request", "steps"}:
            raise ValueError("Supply title, request and steps")
        for key in ("title", "request"):
            if not isinstance(args[key], str) or not args[key].strip() or len(args[key]) > 4000:
                raise ValueError(f"invalid {key}")
        steps = args["steps"]
        if not isinstance(steps, list) or not 1 <= len(steps) <= 12:
            raise ValueError("steps must contain 1 to 12 items")
        normalized = []
        for index, step in enumerate(steps):
            if (not isinstance(step, dict) or set(step) != {"task", "on_failure"}
                    or not isinstance(step["task"], str) or not step["task"].strip()
                    or len(step["task"]) > 4000 or step["on_failure"] not in {"stop", "continue"}):
                raise ValueError("each step requires task and on_failure=stop|continue")
            normalized.append({**step, "id": str(index + 1), "status": "pending"})
        existing = self._db.execute(
            "SELECT id FROM task_plans WHERE source_turn_id=? AND title=?", (turn_id, args["title"])
        ).fetchone()
        if existing:
            plan = self.task_plan(existing[0])
            original = [{k: s[k] for k in ("task", "on_failure")} for s in plan["steps"]]
            if original != steps or plan["request"] != args["request"]:
                raise ValueError("plan already exists with different contents")
            return plan
        plan_id = uuid.uuid4().hex
        now = time.time()
        with self._db:
            self._db.execute(
                "INSERT INTO task_plans (id,source_turn_id,channel,title,request,status,steps_json,step_index,created_at,updated_at) VALUES (?,?,?,?,?,'draft',?,0,?,?)",
                (plan_id, turn_id, channel, args["title"], args["request"], json.dumps(normalized), now, now),
            )
        return self.task_plan(plan_id)

    def update_task_plan(self, plan_id, channel, version, steps):
        plan = self.task_plan(plan_id)
        if plan is None or plan["channel"] != channel: raise ValueError("plan not found")
        if plan["version"] != version: raise ValueError("plan version conflict; reload with plan_get")
        if plan["status"] in {"running", "completed", "cancelled", "failed", "blocked"}: raise ValueError("only a non-running plan can be updated")
        if not isinstance(steps, list) or not steps: raise ValueError("steps required")
        normalized=[]
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or not str(step.get("task") or "").strip() or step.get("on_failure") not in {"stop","continue"}: raise ValueError("invalid step")
            normalized.append({"id":str(i+1),"task":str(step["task"]),"on_failure":step["on_failure"],"status":"pending"})
        with self._db: self._db.execute("UPDATE task_plans SET steps_json=?,version=version+1,updated_at=? WHERE id=? AND version=?",(json.dumps(normalized,ensure_ascii=False),time.time(),plan_id,version))
        return self.task_plan(plan_id)

    def cancel_task_plan(self, plan_id, channel):
        plan=self.task_plan(plan_id)
        if plan is None or plan["channel"] != channel: raise ValueError("plan not found")
        with self._db: self._db.execute("UPDATE task_plans SET status='cancelled',updated_at=?,version=version+1 WHERE id=? AND status NOT IN ('completed','cancelled')",(time.time(),plan_id))
        return self.task_plan(plan_id)

    def start_task_plan(self, plan_id, channel, context=None):
        plan = self.task_plan(plan_id)
        if plan is None or plan["channel"] != channel:
            raise ValueError("plan not found in this channel")
        if plan["status"] not in {"draft", "ready", "running"}:
            raise ValueError("plan is stopped or complete; create a new plan for remaining work")
        with self._db:
            self._db.execute("UPDATE task_plans SET status='ready', context_json=?, updated_at=? WHERE id=? AND status='draft'", (json.dumps(context, ensure_ascii=False), time.time(), plan_id))
        return self.task_plan(plan_id)

    def claim_task_plan(self):
        with self._db:
            row = self._db.execute("""SELECT p.id FROM task_plans p JOIN turns t ON t.id=p.source_turn_id
                WHERE p.status='ready' AND t.state='completed' AND NOT EXISTS (SELECT 1 FROM task_plans r WHERE r.status='running') ORDER BY p.created_at LIMIT 1""").fetchone()
            if row is None:
                return None
            self._db.execute("UPDATE task_plans SET status='running', updated_at=? WHERE id=?", (time.time(), row[0]))
        return self.task_plan(row[0])

    def stop_task_plans(self, channel=None):
        with self._db:
            self._db.execute("""UPDATE task_plans SET status='cancelled', updated_at=?
                WHERE status IN ('draft','ready','running') AND (? IS NULL OR channel=?)""", (time.time(), channel, channel))

    def recover_task_plans(self, plan_id=None):
        # Never replay an interrupted step: its external outcome may be unknown.
        with self._db:
            self._db.execute("UPDATE task_plans SET status='blocked', updated_at=? WHERE status='running' AND (? IS NULL OR id=?)", (time.time(), plan_id, plan_id))

    def finish_task_plan_step(self, plan_id, turn_id, outcome, summary, output_refs, abort=False):
        with self._db:
            plan = self.task_plan(plan_id)
            if plan is None or plan["status"] != "running":
                raise ValueError("plan no longer running")
            step = plan["steps"][plan["step_index"]]
            step.update(status=outcome, result=summary, output_refs=output_refs, turn_id=turn_id)
            stopped = abort or outcome == "blocked" or (outcome == "failed" and step["on_failure"] == "stop")
            last = plan["step_index"] + 1 == len(plan["steps"])
            status = ("blocked" if outcome == "blocked" else "failed") if stopped else (
                ("partial" if any(s["status"] == "failed" for s in plan["steps"]) else "completed") if last else "ready"
            )
            record = {"plan_id": plan_id, "step_id": step["id"], "status": outcome,
                      "result": summary, "output_refs": output_refs, "plan_status": status}
            now = time.time()
            self._archive_progress_messages(turn_id, json.dumps([f"plan:{plan_id}"]))
            self._db.execute("""INSERT INTO messages
                (turn_id,role,content,created_at,source_event_ids_json,delivery_state)
                VALUES (?,'assistant',?,?,?,'internal')""",
                (turn_id, json.dumps(record, ensure_ascii=False), now, json.dumps([f"plan-step-record:{turn_id}"])))
            self._db.execute("UPDATE task_plans SET status=?,steps_json=?,step_index=?,updated_at=? WHERE id=?",
                (status, json.dumps(plan["steps"], ensure_ascii=False), plan["step_index"] + 1, now, plan_id))
            self._db.execute("UPDATE turns SET state='completed',stage='completed',updated_at=? WHERE id=?", (now, turn_id))
        return record
