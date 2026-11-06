import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import yaml

from momoi.config.models import WebhookConfig
from momoi.models import AgentReply
from momoi.storage import Store
from momoi.webhooks.catalog import WorkflowError, bind_workflow, load_catalog
from momoi.webhooks.service import WebhookService


def camera_workflow():
    return {
        "version": 1, "id": "camera-event",
        "inputs": {"event_prompt": {"type": "string", "required": True},
                   "rtmp_url": {"type": "string", "required": True}},
        "steps": [{"id": "notify", "uses": "message", "prompt": "${inputs.event_prompt}"},
                  {"id": "clip", "uses": "video", "source": "${inputs.rtmp_url}",
                   "duration_seconds": 30, "send_as": "file"}],
    }


class VideoCatalogTests(unittest.TestCase):
    def test_catalog_validates_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = root / "executors.yaml"
            executor.write_text("version: 1\nexecutors: {}\n")
            workflows = root / "workflows"
            workflows.mkdir()
            target = workflows / "camera.yaml"
            for duration in (0, 121, True, 1.5):
                workflow = camera_workflow()
                workflow["steps"][1]["duration_seconds"] = duration
                target.write_text(yaml.safe_dump(workflow))
                with self.assertRaises(WorkflowError):
                    load_catalog(workflows, executor)
            workflow = camera_workflow()
            workflow["steps"].append({**workflow["steps"][1], "id": "second"})
            target.write_text(yaml.safe_dump(workflow))
            with self.assertRaisesRegex(WorkflowError, "at most one"):
                load_catalog(workflows, executor)
            target.write_text(yaml.safe_dump(camera_workflow()))
            catalog, executors = load_catalog(workflows, executor)
            for source in ("file:///etc/passwd", "concat:http://a|http://b", "pipe:0", "ftp://example/a", "http://"):
                with self.assertRaisesRegex(WorkflowError, "allowed absolute URL"):
                    bind_workflow(catalog["camera-event"], executors,
                                  {"event_prompt": "event", "rtmp_url": source}, {})
            for scheme in ("rtmp", "rtmps", "rtsp", "http", "https"):
                plan = bind_workflow(catalog["camera-event"], executors,
                                     {"event_prompt": "event", "rtmp_url": f"{scheme}://example/live"}, {})
                self.assertEqual(plan["steps"][1]["source"], f"{scheme}://example/live")


class VideoServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        workflows = self.root / "workflows"
        workflows.mkdir()
        (workflows / "camera.yaml").write_text(yaml.safe_dump(camera_workflow()))
        (workflows / "event.yaml").write_text(yaml.safe_dump({
            "version": 1, "id": "event", "inputs": {},
            "steps": [{"id": "notify", "uses": "message", "prompt": "ordinary"}],
        }))
        executors = self.root / "executors.yaml"
        executors.write_text("version: 1\nexecutors: {}\n")
        self.store = Store(self.root / "momoi.sqlite3")
        self.message_gate = asyncio.Event()
        self.record_gate = asyncio.Event()
        self.prompts = []
        self.message_error = False

        async def complete(prompt, turn):
            self.prompts.append(prompt)
            if prompt != "ordinary":
                await self.message_gate.wait()
            if self.message_error:
                raise RuntimeError("model unavailable")
            return AgentReply([])

        self.service = WebhookService(
            WebhookConfig(workflows=workflows, executors=executors), {},
            self.store, complete, lambda: None, "onebot",
        )

        async def record(source, run_id, duration, **kwargs):
            await self.record_gate.wait()
            path = self.service.recorder.directory / f"{run_id}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"validated recorder fixture")
            return {"path": str(path), "duration_seconds": 30, "size_bytes": path.stat().st_size}

        self.service.recorder.record = AsyncMock(side_effect=record)

    async def asyncTearDown(self):
        tasks = list(self.service._video_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.store.close()
        self.temp.cleanup()

    async def post(self, workflow="camera-event", key="same-event"):
        body = {"event_prompt": "camera event", "rtmp_url": "rtmp://camera.test/live"} if workflow == "camera-event" else {}
        response = await self.service._post(SimpleNamespace(
            match_info={"workflow_id": workflow}, headers={"Idempotency-Key": key},
            json=AsyncMock(return_value=body),
        ))
        self.assertEqual(response.status, 202)
        return json.loads(response.text)["run_id"]

    async def eventually(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.01)

    async def test_recording_finishes_first_and_waits_for_message(self):
        run_id = await self.post()
        self.record_gate.set()
        message = asyncio.create_task(self.service._execute(self.store.claim_webhook_run(), asyncio.Event()))
        await self.eventually(lambda: self.store.webhook_step(run_id, 1)["result"].get("status") == "recorded")
        self.assertEqual(self.store.due_outbox(), [])
        self.assertIn("正在后台录制", self.prompts[0])
        self.message_gate.set()
        await message
        await self.eventually(lambda: bool(self.store.due_outbox()))
        self.assertEqual(self.store.webhook_run(run_id)["state"], "waiting_delivery")
        outbox = self.store.due_outbox()[0]
        self.store._db.execute("UPDATE outbox SET state='sent' WHERE id=?", (outbox.id,))
        self.store._db.commit()
        await self.eventually(lambda: self.store.webhook_run(run_id)["state"] == "succeeded")

    async def test_message_finishes_first_and_duplicate_does_not_record_twice(self):
        run_id = await self.post()
        self.assertEqual(await self.post(), run_id)
        self.message_gate.set()
        await self.service._execute(self.store.claim_webhook_run(), asyncio.Event())
        self.assertEqual(self.store.due_outbox(), [])
        self.record_gate.set()
        await self.eventually(lambda: bool(self.store.due_outbox()))
        self.assertEqual(self.service.recorder.record.await_count, 1)

    async def test_failed_message_still_sends_video(self):
        run_id = await self.post()
        self.message_error = True
        self.message_gate.set()
        self.record_gate.set()
        await self.service._execute(self.store.claim_webhook_run(), asyncio.Event())
        await self.eventually(lambda: bool(self.store.due_outbox()))
        self.assertEqual(self.store.webhook_step(run_id, 0)["state"], "failed")
        self.assertEqual(self.store.webhook_run(run_id)["state"], "waiting_delivery")

    async def test_stop_cancels_recording(self):
        run_id = await self.post()
        await self.eventually(lambda: self.service.recorder.record.await_count == 1)
        self.service.stop_active()
        await self.eventually(lambda: self.store.webhook_step(run_id, 1)["state"] == "failed")
        self.assertEqual(self.store.due_outbox(), [])

    async def test_message_retry_succeeds_without_video(self):
        await self.notification_retry_succeeds("event")

    async def test_message_retry_keeps_recorded_video_waiting(self):
        await self.notification_retry_succeeds("camera-event")

    async def notification_retry_succeeds(self, workflow):
        self.service.complete_turn = AsyncMock(return_value=AgentReply(["通知"]))
        self.record_gate.set()
        run_id = await self.post(workflow)
        execute = asyncio.create_task(self.service._execute(
            self.store.claim_webhook_run(), asyncio.Event(),
        ))
        try:
            await self.eventually(lambda: self.store.webhook_step(run_id, 0)["state"] == "waiting_delivery")
            outbox_id, = self.store.webhook_step(run_id, 0)["result"]["outbox_ids"]
            self.assertTrue(self.store.mark_sending(outbox_id))
            self.store.mark_ambiguous(outbox_id, 1, "response_lost")
            self.assertEqual(self.store.webhook_delivery_state(run_id, 0), "pending")
            # Allow the real delivery waiter to observe the retryable outcome.
            await asyncio.sleep(0.55)
            self.assertFalse(execute.done())
            self.assertEqual(self.store.webhook_step(run_id, 0)["state"], "waiting_delivery")
            if workflow == "camera-event":
                self.assertEqual(self.store.webhook_step(run_id, 1)["result"]["status"], "recorded")
            self.assertTrue(self.store.mark_sending(outbox_id))
            self.store.mark_sent(outbox_id)
            await asyncio.wait_for(execute, 3)
            self.assertEqual(self.store.webhook_step(run_id, 0)["state"], "succeeded")
            if workflow == "camera-event":
                await self.eventually(lambda: bool(self.store.due_outbox()))
                video, = self.store.due_outbox()
                self.store.mark_sent(video.id)
                await self.eventually(lambda: self.store.webhook_run(run_id)["state"] == "succeeded")
            self.assertEqual(self.store.webhook_run(run_id)["state"], "succeeded")
        finally:
            execute.cancel()
            await asyncio.gather(execute, return_exceptions=True)

    async def test_stop_cancels_recorded_clip_before_queued_notification(self):
        run_id = await self.post()
        self.record_gate.set()
        await self.eventually(lambda: self.store.webhook_step(run_id, 1)["result"].get("status") == "recorded")
        monitor = self.service._video_tasks[run_id]
        self.service.stop_active()
        await asyncio.gather(monitor, return_exceptions=True)
        self.assertEqual(self.store.webhook_step(run_id, 1)["error"], "recording_cancelled")
        self.message_gate.set()
        await self.service._execute(self.store.claim_webhook_run(), asyncio.Event())
        self.assertEqual(self.store.webhook_run(run_id)["state"], "failed")
        self.assertEqual(self.store.due_outbox(), [])
        self.assertTrue((self.service.recorder.directory / f"{run_id}.mp4").exists())

    async def test_stop_settles_active_notification_and_recorded_clip(self):
        run_id = await self.post()
        self.record_gate.set()
        worker = asyncio.create_task(self.service.run_worker(asyncio.Event()))
        try:
            await self.eventually(lambda: self.store.webhook_step(run_id, 0)["state"] == "running"
                and self.store.webhook_step(run_id, 1)["result"].get("status") == "recorded")
            self.service.stop_active()
            await self.eventually(lambda: self.store.webhook_run(run_id)["state"] == "failed")
            await self.eventually(lambda: run_id not in self.service._video_tasks)
            self.assertEqual(self.store.webhook_step(run_id, 0)["state"], "failed")
            self.assertEqual(self.store.webhook_step(run_id, 1)["state"], "failed")
            self.assertEqual(self.store.due_outbox(), [])
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_stop_preserves_enqueued_video_delivery_monitor(self):
        run_id = await self.post()
        self.message_gate.set()
        self.record_gate.set()
        await self.service._execute(self.store.claim_webhook_run(), asyncio.Event())
        await self.eventually(lambda: bool(self.store.due_outbox()))
        monitor = self.service._video_tasks[run_id]
        self.assertIsNone(self.service._active_run)
        self.service.stop_active()
        self.assertEqual(monitor.cancelling(), 0)
        self.store.mark_sent(self.store.due_outbox()[0].id)
        await self.eventually(lambda: self.store.webhook_run(run_id)["state"] == "succeeded")
        self.assertEqual(self.store.webhook_step(run_id, 1)["state"], "succeeded")

    async def test_uncertain_delivery_is_terminal_without_resending(self):
        run_id = await self.post()
        self.message_gate.set()
        self.record_gate.set()
        await self.service._execute(self.store.claim_webhook_run(), asyncio.Event())
        await self.eventually(lambda: bool(self.store.due_outbox()))
        self.store._db.execute("UPDATE outbox SET state='ambiguous'")
        self.store._db.commit()
        await self.eventually(lambda: self.store.webhook_run(run_id)["state"] == "ambiguous")
        self.assertEqual(self.store._db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)

    async def test_recording_failure_does_not_skip_notification(self):
        from momoi.media.recorder import RecorderError

        self.service.recorder.record = AsyncMock(side_effect=RecorderError("recording_timeout"))
        run_id = await self.post()
        await self.eventually(lambda: self.store.webhook_step(run_id, 1)["state"] == "failed")
        self.message_gate.set()
        await self.service._execute(self.store.claim_webhook_run(), asyncio.Event())
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self.store.webhook_run(run_id)["state"], "failed")

    async def test_shutdown_preserves_recorded_and_enqueued_stages(self):
        run_id = await self.post()
        self.record_gate.set()
        await self.eventually(lambda: self.store.webhook_step(run_id, 1)["result"].get("status") == "recorded")
        task = self.service._video_tasks[run_id]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.store.webhook_step(run_id, 1)["result"]["status"], "recorded")
        self.assertEqual(self.store.webhook_step(run_id, 1)["state"], "running")
        run = self.store.recoverable_webhook_video_runs()[0]
        self.service._start_video(run_id, run["plan"], recovering=True)
        self.message_gate.set()
        await self.service._execute(self.store.claim_webhook_run(), asyncio.Event())
        await self.eventually(lambda: bool(self.store.due_outbox()))
        task = self.service._video_tasks[run_id]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.store.webhook_step(run_id, 1)["result"]["status"], "enqueued")
        self.assertEqual(self.store.webhook_step(run_id, 1)["state"], "waiting_delivery")
        self.assertEqual(self.service.recorder.record.await_count, 1)

    async def test_worker_handles_other_workflow_while_recording(self):
        camera = await self.post()
        self.message_gate.set()
        ordinary = await self.post("event", "ordinary")
        stop = asyncio.Event()
        worker = asyncio.create_task(self.service.run_worker(stop))
        try:
            await self.eventually(lambda: self.store.webhook_run(ordinary)["state"] == "succeeded")
            self.assertEqual(self.store.webhook_step(camera, 1)["result"]["status"], "recording")
            self.assertEqual(self.prompts[-1], "ordinary")
        finally:
            stop.set()
            self.service.changed.set()
            await worker


class AgentVideoServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        workflows = self.root / "workflows"
        workflows.mkdir()
        workflow = camera_workflow()
        workflow["steps"][1]["deliver"] = "agent"
        (workflows / "camera.yaml").write_text(yaml.safe_dump(workflow))
        executors = self.root / "executors.yaml"
        executors.write_text("version: 1\nexecutors: {}\n")
        self.store = Store(self.root / "momoi.sqlite3")
        self.prompts = []
        self.replies = {}
        self.record_error = None

        async def complete(prompt, turn):
            self.prompts.append((turn, prompt))
            return self.replies.get(turn, AgentReply([]))

        self.service = WebhookService(
            WebhookConfig(workflows=workflows, executors=executors), {},
            self.store, complete, lambda: None, "onebot",
        )

        async def record(source, run_id, duration, **kwargs):
            if self.record_error:
                from momoi.media.recorder import RecorderError
                raise RecorderError(self.record_error)
            path = self.service.recorder.directory / f"{run_id}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"validated recorder fixture")
            return {"path": str(path), "duration_seconds": 12, "size_bytes": path.stat().st_size}

        self.service.recorder.record = AsyncMock(side_effect=record)

    async def asyncTearDown(self):
        tasks = list(self.service._video_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.store.close()
        self.temp.cleanup()

    async def run_event(self):
        response = await self.service._post(SimpleNamespace(
            match_info={"workflow_id": "camera-event"}, headers={"Idempotency-Key": "door"},
            json=AsyncMock(return_value={"event_prompt": "door event",
                                         "rtmp_url": "https://camera.test/event.m3u8"}),
        ))
        run_id = json.loads(response.text)["run_id"]
        await self.service._execute(self.store.claim_webhook_run(), asyncio.Event())
        return run_id

    async def eventually(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.01)

    async def test_agent_reviews_finished_video_and_attaches_it(self):
        path = str(self.service.recorder.directory / "RUN.mp4")
        attachment = {"segments": [{"type": "file", "data": {"file": path, "name": "RUN.mp4"}}]}
        self.service._video_changed.set()
        run_id = None

        async def complete(prompt, turn):
            self.prompts.append((turn, prompt))
            if turn.endswith(":1"):
                return AgentReply(["门口是快递员", json.loads(json.dumps(attachment).replace("RUN", run_id))])
            return AgentReply([])

        self.service.complete_turn = complete
        response = await self.service._post(SimpleNamespace(
            match_info={"workflow_id": "camera-event"}, headers={"Idempotency-Key": "door"},
            json=AsyncMock(return_value={"event_prompt": "door event",
                                         "rtmp_url": "https://camera.test/event.m3u8"}),
        ))
        run_id = json.loads(response.text)["run_id"]
        await self.service._execute(self.store.claim_webhook_run(), asyncio.Event())
        await self.eventually(lambda: len(self.prompts) == 2)
        notify, review = self.prompts
        self.assertIn("由你判断", notify[1])
        self.assertEqual(review[0], f"webhook:{run_id}:1")
        self.assertIn(str(self.service.recorder.directory / f"{run_id}.mp4"), review[1])
        rows = lambda: self.store._db.execute(
            "SELECT id, text, media_path FROM outbox ORDER BY id"
        ).fetchall()
        await self.eventually(lambda: len(rows()) == 2)
        self.assertFalse(any(row["text"].startswith("[视频") for row in rows()))
        self.assertEqual(rows()[1]["media_path"],
                         str(self.service.recorder.directory / f"{run_id}.mp4"))
        step = self.store.webhook_step(run_id, 1)
        self.assertEqual(step["result"]["status"], "enqueued")
        self.assertTrue(step["result"]["path"].endswith(f"{run_id}.mp4"))
        self.store._db.execute("UPDATE outbox SET state='sent'")
        self.store._db.commit()
        await self.eventually(lambda: self.store.webhook_run(run_id)["state"] == "succeeded")

    async def test_agent_may_stay_silent_after_review(self):
        run_id = await self.run_event()
        await self.eventually(lambda: self.store.webhook_run(run_id)["state"] == "succeeded")
        self.assertEqual(len(self.prompts), 2)
        self.assertEqual(self.store.due_outbox(), [])
        self.assertEqual(self.store.webhook_step(run_id, 1)["result"]["status"], "reviewed")

    async def test_agent_hears_about_failed_recording(self):
        self.record_error = "recording_invalid_media:InvalidDataError"
        run_id = await self.run_event()
        await self.eventually(lambda: self.store.webhook_run(run_id)["state"] == "failed")
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("录制失败", self.prompts[1][1])
        self.assertNotIn(".mp4", self.prompts[1][1])
        step = self.store.webhook_step(run_id, 1)
        self.assertEqual((step["state"], step["error"]), ("failed", self.record_error))

    async def test_catalog_rejects_unknown_delivery(self):
        workflow = camera_workflow()
        workflow["steps"][1]["deliver"] = "maybe"
        (self.root / "workflows" / "camera.yaml").write_text(yaml.safe_dump(workflow))
        with self.assertRaisesRegex(WorkflowError, "deliver"):
            load_catalog(self.root / "workflows", self.root / "executors.yaml")
