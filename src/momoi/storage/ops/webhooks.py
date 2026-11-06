import json
import sqlite3
import time
import uuid
from pathlib import Path

from ...models import AgentReply
from ..core.integrity import decode_stored_json
from ..core.turn_workflow import turn_workflow_kind_sql


class WebhookStore:
    def create_webhook_run(
        self,
        workflow_id: str,
        idempotency_key: str | None,
        plan: dict[str, object],
    ) -> tuple[dict[str, object], bool]:
        if idempotency_key is not None:
            existing = self._db.execute(
                """SELECT id, workflow_id, state FROM webhook_runs
                   WHERE workflow_id=? AND idempotency_key=?""",
                (workflow_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return dict(existing), False
        run_id = uuid.uuid4().hex
        now = time.time()
        steps = plan.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("webhook plan needs steps")
        try:
            with self._db:
                self._db.execute(
                    """INSERT INTO webhook_runs
                       (id, workflow_id, idempotency_key, plan_json, state,
                        current_step, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'queued', 0, ?, ?)""",
                    (
                        run_id,
                        workflow_id,
                        idempotency_key,
                        json.dumps(plan, ensure_ascii=False, separators=(",", ":")),
                        now,
                        now,
                    ),
                )
                self._db.executemany(
                    """INSERT INTO webhook_steps
                       (run_id, step_index, step_id, kind, state)
                       VALUES (?, ?, ?, ?, 'queued')""",
                    (
                        (run_id, index, str(step["id"]), str(step["uses"]))
                        for index, step in enumerate(steps)
                    ),
                )
        except sqlite3.IntegrityError:
            if idempotency_key is None:
                raise
            existing = self._db.execute(
                """SELECT id, workflow_id, state FROM webhook_runs
                   WHERE workflow_id=? AND idempotency_key=?""",
                (workflow_id, idempotency_key),
            ).fetchone()
            if existing is None:
                raise
            return dict(existing), False
        return {"id": run_id, "workflow_id": workflow_id, "state": "queued"}, True

    def webhook_run(self, run_id: str) -> dict[str, object] | None:
        row = self._db.execute(
            """SELECT id, workflow_id, state, current_step, error,
                      created_at, updated_at
               FROM webhook_runs WHERE id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        steps = self._db.execute(
            """SELECT step_index, step_id, kind, state, error, started_at, completed_at
               FROM webhook_steps WHERE run_id=? ORDER BY step_index""",
            (run_id,),
        ).fetchall()
        return {
            "run_id": row["id"],
            "workflow": row["workflow_id"],
            "state": row["state"],
            "current_step": row["current_step"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "steps": [dict(step) for step in steps],
        }

    def claim_webhook_run(self) -> dict[str, object] | None:
        with self._db:
            row = self._db.execute(
                """SELECT id, workflow_id, plan_json, state, current_step
                   FROM webhook_runs
                   WHERE state IN ('queued', 'waiting_delivery')
                     AND (NOT EXISTS (
                         SELECT 1 FROM webhook_steps v
                         WHERE v.run_id=webhook_runs.id AND v.kind='video'
                     ) OR EXISTS (
                         SELECT 1 FROM webhook_steps s
                         WHERE s.run_id=webhook_runs.id AND s.kind!='video'
                           AND s.state NOT IN ('succeeded', 'failed', 'ambiguous')
                     ))
                   ORDER BY created_at LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            if row["state"] == "queued":
                self._db.execute(
                    "UPDATE webhook_runs SET state='running', updated_at=? WHERE id=?",
                    (time.time(), row["id"]),
                )
        result = dict(row)
        result["state"] = "running" if row["state"] == "queued" else row["state"]
        result["plan"] = json.loads(str(row["plan_json"]))
        return result

    def webhook_step(self, run_id: str, step_index: int) -> dict[str, object] | None:
        row = self._db.execute(
            """SELECT run_id, step_index, step_id, kind, state, result_json, error
               FROM webhook_steps WHERE run_id=? AND step_index=?""",
            (run_id, step_index),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result"] = decode_stored_json(
            row["result_json"],
            entity="webhook_step",
            record_id=f"{run_id}:{step_index}",
            field="result_json",
            expected_type=dict,
            fallback={},
        )
        return result

    def start_webhook_step(self, run_id: str, step_index: int) -> None:
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE webhook_steps SET state='running', started_at=?,
                   completed_at=NULL, error=NULL
                   WHERE run_id=? AND step_index=? AND state='queued'""",
                (now, run_id, step_index),
            )
            self._db.execute(
                """UPDATE webhook_runs SET state='running', current_step=?,
                   error=NULL, updated_at=? WHERE id=?""",
                (step_index, now, run_id),
            )

    def _webhook_workflow_id(self, run_id: str) -> str:
        row = self._db.execute(
            "SELECT workflow_id FROM webhook_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError("webhook run not found")
        return str(row["workflow_id"])

    def record_webhook_event(
        self,
        run_id: str,
        turn_id: str,
        content: str,
    ) -> int:
        text = str(content or "").strip()
        if not text:
            raise ValueError("webhook event content is empty")
        now = time.time()
        source = json.dumps([turn_id], ensure_ascii=False)
        with self._db:
            existing = self._db.execute(
                """SELECT id FROM messages
                   WHERE turn_id=? AND role='event'
                   ORDER BY id LIMIT 1""",
                (turn_id,),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            inserted = self._db.execute(
                """INSERT INTO messages
                   (turn_id, role, content, created_at, source_event_ids_json,
                    delivery_state)
                   VALUES (?, 'event', ?, ?, ?, 'delivered')""",
                (turn_id, text, now, source),
            )
            workflow_id = self._webhook_workflow_id(run_id)
            archive_day = self._archive_day(now)
            self._ensure_runtime_archive(
                archive_kind="webhook",
                archive_day=archive_day,
                episode_key=f"webhook:{workflow_id}:day:{archive_day}",
                turn_id=turn_id,
                title=f"Webhook {workflow_id}",
                now=now,
                recall_values=(text,),
            )
            return int(inserted.lastrowid)

    def commit_webhook_reply(
        self,
        run_id: str,
        step_index: int,
        turn_id: str,
        reply: AgentReply,
        target_channel: str = "",
    ) -> list[int]:
        step = self.webhook_step(run_id, step_index)
        if step is None:
            raise ValueError("webhook step not found")
        if step["state"] in {"waiting_delivery", "succeeded"}:
            result = step["result"]
            return [int(value) for value in result.get("outbox_ids", [])]  # type: ignore[union-attr]
        if step["state"] != "running":
            raise ValueError("webhook message step is not running")
        normalized = [self._outbox_content(message) for message in reply.messages]
        source_ids = [turn_id]
        source = json.dumps(source_ids, ensure_ascii=False)
        now = time.time()
        with self._db:
            progress = self._db.execute(
                """SELECT text, created_at, tool_call_id, part_index
                   FROM turn_progress
                   WHERE turn_id=? ORDER BY created_at, tool_call_id, part_index""",
                (turn_id,),
            ).fetchall()
            self._archive_progress_messages(turn_id, source)
            for index, (text, kind, path, payload) in enumerate(normalized):
                outbox = self._db.execute(
                    """INSERT INTO outbox
                       (turn_id, dedupe_key, text, kind, media_path, payload_json,
                        target_channel)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        turn_id,
                        f"turn:{turn_id}:{index}",
                        text,
                        kind,
                        path,
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        target_channel,
                    ),
                )
                self._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at, source_event_ids_json,
                        outbox_id, delivery_state)
                       VALUES (?, 'assistant', ?, ?, ?, ?, 'queued')""",
                    (turn_id, text, now, source, outbox.lastrowid),
                )
            visible = [str(row["text"]) for row in progress] + [
                text for text, _, _, _ in normalized
            ]
            if visible:
                event = self._db.execute(
                    """SELECT created_at FROM messages
                       WHERE turn_id=? AND role='event'
                       ORDER BY id LIMIT 1""",
                    (turn_id,),
                ).fetchone()
                when = float(event["created_at"]) if event is not None else now
                workflow_id = self._webhook_workflow_id(run_id)
                archive_day = self._archive_day(when)
                self._ensure_runtime_archive(
                    archive_kind="webhook",
                    archive_day=archive_day,
                    episode_key=f"webhook:{workflow_id}:day:{archive_day}",
                    turn_id=turn_id,
                    title=f"Webhook {workflow_id}",
                    now=now,
                    recall_values=(visible,),
                )
            self._apply_mood_update(reply.mood_update, now)
            outbox_ids = [
                int(row["id"])
                for row in self._db.execute(
                    "SELECT id FROM outbox WHERE turn_id=? ORDER BY id", (turn_id,)
                ).fetchall()
            ]
            result = {"outbox_ids": outbox_ids}
            if step["kind"] == "video":
                # The recording's path and outcome stay with the step for cleanup.
                result = {**step["result"], **result,
                          "status": "enqueued" if outbox_ids else "reviewed"}
            result_json = json.dumps(result, separators=(",", ":"))
            step_state = "waiting_delivery" if outbox_ids else "succeeded"
            self._db.execute(
                """UPDATE webhook_steps SET state=?, result_json=?, completed_at=?
                   WHERE run_id=? AND step_index=?""",
                (
                    step_state,
                    result_json,
                    None if outbox_ids else now,
                    run_id,
                    step_index,
                ),
            )
            self._db.execute(
                """UPDATE webhook_runs SET state=?, current_step=?, updated_at=?
                   WHERE id=?""",
                (
                    "waiting_delivery" if outbox_ids else "running",
                    step_index if outbox_ids else step_index + 1,
                    now,
                    run_id,
                ),
            )
            if step["kind"] == "video":
                self._refresh_webhook_video_run(run_id)
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   failure_reason=NULL, updated_at=? WHERE id=?""",
                (now, turn_id),
            )
        return outbox_ids

    def webhook_delivery_state(self, run_id: str, step_index: int) -> str:
        step = self.webhook_step(run_id, step_index)
        if step is None:
            return "failed"
        result = step["result"]
        ids = [int(value) for value in result.get("outbox_ids", [])]  # type: ignore[union-attr]
        if not ids:
            return "failed"
        placeholders = ",".join("?" for _ in ids)
        rows = self._db.execute(
            f"SELECT id, state FROM outbox WHERE id IN ({placeholders})", ids
        ).fetchall()
        states = {str(row["state"]) for row in rows}
        if any(row["state"] in {"ambiguous", "uncertain"}
               and not self.outbox_dispatchable(int(row["id"])) for row in rows):
            return "ambiguous"
        if len(rows) != len(ids) or states & {"failed", "superseded"}:
            return "failed"
        return "succeeded" if states == {"sent"} else "pending"

    def finish_webhook_step(
        self,
        run_id: str,
        step_index: int,
        state: str,
        result: dict[str, object],
        error: str | None,
    ) -> None:
        if state not in {"succeeded", "failed", "ambiguous"}:
            raise ValueError("invalid webhook terminal state")
        step = self.webhook_step(run_id, step_index)
        if step is not None and step["kind"] == "video":
            result = {**step["result"], **result, "status": state}
        now = time.time()
        with self._db:
            if result:
                result_json = json.dumps(
                    result, ensure_ascii=False, separators=(",", ":")
                )
                self._db.execute(
                    """UPDATE webhook_steps SET state=?, result_json=?, error=?, completed_at=?
                       WHERE run_id=? AND step_index=?""",
                    (state, result_json, error, now, run_id, step_index),
                )
            else:
                self._db.execute(
                    """UPDATE webhook_steps SET state=?, error=?, completed_at=?
                       WHERE run_id=? AND step_index=?""",
                    (state, error, now, run_id, step_index),
                )
            run_state = "running" if state == "succeeded" else state
            self._db.execute(
                """UPDATE webhook_runs SET state=?, current_step=?, error=?, updated_at=?
                   WHERE id=?""",
                (
                    run_state,
                    step_index + 1 if state == "succeeded" else step_index,
                    error,
                    now,
                    run_id,
                ),
            )
            if self._webhook_has_video(run_id):
                self._refresh_webhook_video_run(run_id)

    def complete_webhook_run(self, run_id: str) -> None:
        with self._db:
            if self._webhook_has_video(run_id):
                self._refresh_webhook_video_run(run_id)
                return
            self._db.execute(
                """UPDATE webhook_runs SET state='succeeded', error=NULL, updated_at=?
                   WHERE id=? AND NOT EXISTS (
                       SELECT 1 FROM webhook_steps
                       WHERE run_id=? AND state!='succeeded'
                   )""",
                (time.time(), run_id, run_id),
            )

    def fail_webhook_run(self, run_id: str, error: str) -> None:
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE webhook_runs SET state='failed', error=?, updated_at=?
                   WHERE id=? AND state NOT IN ('succeeded', 'ambiguous')""",
                (error[:500], now, run_id),
            )
            self._db.execute(
                """UPDATE webhook_steps SET state='failed', error=?, completed_at=?,
                   result_json=CASE WHEN kind='video'
                       THEN json_set(result_json, '$.status', 'failed') ELSE result_json END
                   WHERE run_id=? AND state IN ('queued', 'running', 'waiting_delivery')""",
                (error[:500], now, run_id),
            )

    @property
    def webhook_media_directory(self) -> Path:
        row = self._db.execute("PRAGMA database_list").fetchone()
        return Path(row["file"]).parent / "media" / "webhook"

    def _webhook_has_video(self, run_id: str) -> bool:
        return self._db.execute(
            "SELECT 1 FROM webhook_steps WHERE run_id=? AND kind='video'",
            (run_id,),
        ).fetchone() is not None

    def _refresh_webhook_video_run(self, run_id: str) -> None:
        self._db.execute(
            """UPDATE webhook_steps SET state='failed', error='previous_step_failed', completed_at=?
               WHERE run_id=? AND kind!='video' AND state='queued'
                 AND EXISTS (SELECT 1 FROM webhook_steps prior
                     WHERE prior.run_id=webhook_steps.run_id AND prior.kind='exec'
                       AND prior.state IN ('failed', 'ambiguous')
                       AND prior.step_index < webhook_steps.step_index)""",
            (time.time(), run_id),
        )
        steps = self._db.execute(
            "SELECT state, error, kind, step_index FROM webhook_steps WHERE run_id=? ORDER BY step_index",
            (run_id,),
        ).fetchall()
        terminal = {"succeeded", "failed", "ambiguous"}
        states = {row["state"] for row in steps}
        if states <= terminal:
            state = "ambiguous" if "ambiguous" in states else (
                "failed" if "failed" in states else "succeeded"
            )
            error = next((row["error"] for row in steps if row["error"]), None)
        else:
            state = "waiting_delivery" if "waiting_delivery" in states else "queued"
            error = None
        current = next((row["step_index"] for row in steps
                        if row["kind"] != "video" and row["state"] not in terminal), len(steps))
        self._db.execute(
            "UPDATE webhook_runs SET state=?, current_step=?, error=?, updated_at=? WHERE id=?",
            (state, current, error, time.time(), run_id),
        )

    def begin_webhook_video(
        self, run_id: str, step_index: int, part_path: str = ""
    ) -> bool:
        with self._db:
            result = {"status": "recording"}
            if part_path:
                result["part_path"] = part_path
            cursor = self._db.execute(
                """UPDATE webhook_steps SET state='running', result_json=?, started_at=?
                   WHERE run_id=? AND step_index=? AND kind='video' AND state='queued'""",
                (json.dumps(result), time.time(), run_id, step_index),
            )
        return bool(cursor.rowcount)

    def record_webhook_video(
        self, run_id: str, step_index: int, asset: dict[str, object]
    ) -> bool:
        with self._db:
            cursor = self._db.execute(
                """UPDATE webhook_steps SET result_json=? WHERE run_id=? AND step_index=?
                   AND kind='video' AND state='running'
                   AND json_extract(result_json, '$.status')='recording'""",
                (json.dumps({**asset, "status": "recorded"}), run_id, step_index),
            )
        return bool(cursor.rowcount)

    def fail_webhook_video_recording(self, run_id: str, step_index: int, error: str) -> bool:
        """Keep a failed agent-delivered recording open so the agent hears about it."""
        with self._db:
            cursor = self._db.execute(
                """UPDATE webhook_steps SET result_json=json_set(
                       result_json, '$.status', 'record_failed', '$.record_error', ?)
                   WHERE run_id=? AND step_index=? AND kind='video' AND state='running'
                     AND json_extract(result_json, '$.status')='recording'""",
                (error, run_id, step_index),
            )
        return bool(cursor.rowcount)

    def webhook_messages_settled(self, run_id: str) -> bool:
        return self._db.execute(
            """SELECT 1 FROM webhook_steps WHERE run_id=? AND kind='message'
               AND state NOT IN ('succeeded', 'failed', 'ambiguous') LIMIT 1""",
            (run_id,),
        ).fetchone() is None

    def begin_webhook_video_review(self, run_id: str, step_index: int) -> bool:
        with self._db:
            cursor = self._db.execute(
                """UPDATE webhook_steps SET result_json=json_set(result_json, '$.status', 'reviewing')
                   WHERE run_id=? AND step_index=? AND kind='video' AND state='running'
                     AND json_extract(result_json, '$.status') IN
                         ('recorded', 'record_failed', 'reviewing')""",
                (run_id, step_index),
            )
        return bool(cursor.rowcount)

    def enqueue_webhook_video(
        self, run_id: str, step_index: int, target_channel: str = ""
    ) -> list[int]:
        with self._db:
            step = self.webhook_step(run_id, step_index)
            if step is None or step["kind"] != "video":
                raise ValueError("webhook video step not found")
            result = step["result"]
            if result.get("outbox_ids"):
                return [int(value) for value in result["outbox_ids"]]
            if step["state"] != "running" or result.get("status") != "recorded":
                return []
            unsettled = self._db.execute(
                """SELECT 1 FROM webhook_steps WHERE run_id=? AND kind='message'
                   AND state NOT IN ('succeeded', 'failed', 'ambiguous') LIMIT 1""",
                (run_id,),
            ).fetchone()
            if unsettled is not None:
                return []
            run = self._db.execute(
                "SELECT plan_json, created_at FROM webhook_runs WHERE id=?", (run_id,)
            ).fetchone()
            plan = json.loads(run["plan_json"])
            mode = plan["steps"][step_index].get("send_as", "file")
            if mode not in {"file", "video"}:
                raise ValueError("invalid video send_as")
            path = str(result["path"])
            turn_id = f"webhook:{run_id}:{step_index}"
            segment = {"type": mode, "send_as": mode, "data": {
                "file": path, "name": f"camera-{int(run['created_at'])}.mp4"
            }}
            payload = {"segments": [segment]}
            text = f"[视频: {segment['data']['name']}]"
            self._db.execute(
                """INSERT INTO outbox
                   (turn_id, dedupe_key, text, kind, media_path, payload_json, target_channel)
                   VALUES (?, ?, ?, 'message', ?, ?, ?)
                   ON CONFLICT(dedupe_key) DO NOTHING""",
                (turn_id, f"webhook-video:{run_id}:{step_index}", text, path,
                 json.dumps(payload, ensure_ascii=False), target_channel),
            )
            outbox_id = int(self._db.execute(
                "SELECT id FROM outbox WHERE dedupe_key=?",
                (f"webhook-video:{run_id}:{step_index}",),
            ).fetchone()["id"])
            self._db.execute(
                """INSERT INTO messages
                   (turn_id, role, content, created_at, source_event_ids_json,
                    outbox_id, delivery_state)
                   SELECT ?, 'assistant', ?, ?, ?, ?, 'queued'
                   WHERE NOT EXISTS (SELECT 1 FROM messages WHERE outbox_id=?)""",
                (turn_id, text, time.time(), json.dumps([turn_id]), outbox_id, outbox_id),
            )
            self._db.execute(
                """UPDATE webhook_steps SET state='waiting_delivery', result_json=?
                   WHERE run_id=? AND step_index=?""",
                (json.dumps({**result, "status": "enqueued", "outbox_ids": [outbox_id]}),
                 run_id, step_index),
            )
            self._refresh_webhook_video_run(run_id)
        return [outbox_id]

    def recoverable_webhook_video_runs(self) -> list[dict[str, object]]:
        rows = self._db.execute(
            """SELECT DISTINCT r.id, r.workflow_id, r.plan_json, r.state, r.current_step
               FROM webhook_runs r JOIN webhook_steps s ON s.run_id=r.id
               WHERE s.kind='video' AND s.state IN ('queued','running','waiting_delivery')"""
        ).fetchall()
        return [{**dict(row), "plan": json.loads(row["plan_json"])} for row in rows]

    def cleanup_webhook_video_assets(
        self, directory: Path, success_days: float = 1, failed_days: float = 7
    ) -> int:
        now = time.time()
        removed = 0
        for row in self._db.execute(
            """SELECT run_id, state, result_json, completed_at FROM webhook_steps
               WHERE kind='video' AND state IN ('succeeded','failed','ambiguous')
                 AND completed_at IS NOT NULL"""
        ).fetchall():
            days = success_days if row["state"] == "succeeded" else failed_days
            if now - row["completed_at"] < days * 86400:
                continue
            result = json.loads(row["result_json"])
            if not result.get("path"):
                continue
            path = Path(result["path"])
            # Only files owned by this recorder may be removed.
            if path.parent.resolve() != directory.resolve() or path.name != f"{row['run_id']}.mp4":
                continue
            if not path.exists():
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            removed += 1
        return removed
