import asyncio
import hmac
import json
import logging
import os
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import monotonic
from typing import Any

from aiohttp import web

from .catalog import WorkflowError, bind_workflow, load_catalog
from ..tools.process import run_process
from ..config.models import WebhookConfig
from ..observability.context import log_context
from ..observability.events import log_event
from ..models import AgentReply
from ..media.recorder import Recorder, RecorderError
from ..storage import Store

logger = logging.getLogger(__name__)


class WebhookService:
    def __init__(
        self,
        config: WebhookConfig,
        channel_variables: dict[str, str],
        store: Store,
        complete_turn: Callable[[str, str], Awaitable[AgentReply]],
        wake_outbox: Callable[[], None],
        primary_channel: str = "",
        *,
        turn_settled: Callable[[str], None] | None = None,
    ) -> None:
        if config.workflows is None or config.executors is None:
            raise WorkflowError("webhook paths are not configured")
        self.config = config
        self.channel_variables = channel_variables
        self.store = store
        self.complete_turn = complete_turn
        self.wake_outbox = wake_outbox
        self.primary_channel = primary_channel
        self.turn_settled = turn_settled
        self.workflows, self.executors = load_catalog(
            config.workflows, config.executors, set(channel_variables)
        )
        self.changed = asyncio.Event()
        self._active_run: asyncio.Task[None] | None = None
        self._owner_stop_requested = False
        self.recorder = Recorder(
            store.webhook_media_directory,
            max_bytes=config.video_max_bytes,
            concurrency=config.video_concurrency,
            allow_transcode=config.video_allow_transcode,
        )
        self._video_tasks: dict[str, asyncio.Task[None]] = {}
        self._video_changed = asyncio.Event()

    def stop_active(self) -> None:
        """Stop a running webhook executor or delivery wait for /stop."""
        if self._active_run is not None and not self._active_run.done():
            self._owner_stop_requested = True
            self._active_run.cancel()
        for run_id, task in self._video_tasks.items():
            run = self.store.webhook_run(run_id)
            if run is None:
                continue
            for step in run["steps"]:
                if step["kind"] == "video":
                    index = int(step["step_index"])
                    record = self.store.webhook_step(run_id, index)
                    status = record["result"].get("status")
                    if status in {None, "recording", "recorded", "record_failed", "reviewing"}:
                        if record["state"] == "queued" or status in {
                            "recorded", "record_failed", "reviewing"
                        }:
                            self.store.finish_webhook_step(
                                run_id, index, "failed", {}, "recording_cancelled"
                            )
                        task.cancel()

    async def run_api(self, stop: asyncio.Event) -> None:
        @web.middleware
        async def authenticate(
            request: web.Request,
            handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
        ) -> web.StreamResponse:
            authorization = request.headers.get("Authorization", "")
            expected = f"Bearer {self.config.token}"
            if not hmac.compare_digest(authorization, expected):
                return web.json_response({"error": "unauthorized"}, status=401)
            return await handler(request)

        app = web.Application(client_max_size=64 * 1024, middlewares=[authenticate])
        app.router.add_post("/webhooks/{workflow_id}", self._post)
        app.router.add_get("/webhook-runs/{run_id}", self._get)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.config.host, self.config.port)
        await site.start()
        log_event(
            logger,
            logging.INFO,
            "webhook_api_started",
            host=self.config.host,
            port=self.config.port,
        )
        try:
            await stop.wait()
        finally:
            await runner.cleanup()

    async def _post(self, request: web.Request) -> web.Response:
        workflow_id = request.match_info["workflow_id"]
        workflow = self.workflows.get(workflow_id)
        if workflow is None:
            return web.json_response({"error": "workflow_not_found"}, status=404)
        try:
            body = await request.json()
            plan = bind_workflow(workflow, self.executors, body, self.channel_variables)
            key = request.headers.get("Idempotency-Key")
            if key is not None and (
                not key or len(key) > 200 or any(ord(char) < 32 for char in key)
            ):
                raise WorkflowError("Idempotency-Key is invalid")
            run, created = self.store.create_webhook_run(workflow_id, key, plan)
        except (json.JSONDecodeError, WorkflowError, TypeError, ValueError) as error:
            return web.json_response(
                {"error": "invalid_request", "detail": str(error)}, status=400
            )
        if created:
            self._start_video(str(run["id"]), plan)
        self.changed.set()
        log_event(
            logger,
            logging.INFO,
            "webhook_run_accepted",
            workflow_id=workflow_id,
            run_id=run["id"],
            created=created,
            state=run["state"],
        )
        return web.json_response(
            {
                "run_id": run["id"],
                "workflow": run["workflow_id"],
                "state": run["state"],
            },
            status=202,
        )

    async def _get(self, request: web.Request) -> web.Response:
        run = self.store.webhook_run(request.match_info["run_id"])
        if run is None:
            return web.json_response({"error": "run_not_found"}, status=404)
        return web.json_response(run)

    async def run_worker(self, stop: asyncio.Event) -> None:
        for run in self.store.recoverable_webhook_video_runs():
            self._start_video(str(run["id"]), run["plan"], recovering=True)
        self.store.cleanup_webhook_video_assets(
            self.recorder.directory, self.config.video_retention_days,
            self.config.video_failed_retention_days,
        )
        try:
            await self._run_worker(stop)
        finally:
            tasks = list(self._video_tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _start_video(
        self, run_id: str, plan: dict[str, Any], *, recovering: bool = False
    ) -> None:
        if run_id in self._video_tasks:
            return
        for index, step in enumerate(plan["steps"]):
            if step["uses"] != "video":
                continue
            task = asyncio.create_task(self._video(run_id, index, step, recovering))
            self._video_tasks[run_id] = task
            task.add_done_callback(lambda _task, key=run_id: self._video_tasks.pop(key, None))

    async def _video(
        self, run_id: str, index: int, step: dict[str, Any], recovering: bool
    ) -> None:
        cancel = threading.Event()
        try:
            record = self.store.webhook_step(run_id, index)
            if record is None or record["state"] in {"succeeded", "failed", "ambiguous"}:
                return
            agent = step.get("deliver", "auto") == "agent"
            if not recovering:
                part = str(self.recorder.directory / f"{run_id}.part.mp4")
                if self.store.begin_webhook_video(run_id, index, part):
                    try:
                        asset = await self.recorder.record(
                            str(step["source"]), run_id, int(step["duration_seconds"]),
                            cancel=cancel,
                        )
                    except RecorderError as error:
                        if not agent or str(error) == "recording_cancelled":
                            raise
                        log_event(logger, logging.WARNING, "webhook_video_failed",
                                  run_id=run_id, error=str(error))
                        self.store.fail_webhook_video_recording(run_id, index, str(error))
                    else:
                        if not self.store.record_webhook_video(run_id, index, asset):
                            (self.recorder.directory / f"{run_id}.mp4").unlink(missing_ok=True)
                            return
            while True:
                self._video_changed.clear()
                record = self.store.webhook_step(run_id, index)
                if record is None or record["state"] in {"succeeded", "failed", "ambiguous"}:
                    return
                status = record["result"].get("status")
                if agent and status in {"recorded", "record_failed", "reviewing"}:
                    if self.store.webhook_messages_settled(run_id):
                        await self._review_video(run_id, index, step)
                        continue
                elif status == "recorded":
                    ids = self.store.enqueue_webhook_video(run_id, index, self.primary_channel)
                    if ids:
                        self.wake_outbox()
                        continue
                elif status == "enqueued":
                    await self._wait_delivery(run_id, index, asyncio.Event())
                    error = record["result"].get("record_error")
                    if error:
                        self.store.finish_webhook_step(run_id, index, "failed", {}, error)
                    return
                try:
                    # Notification completion wakes all clips; recheck persisted
                    # state periodically for updates made outside this worker.
                    await asyncio.wait_for(self._video_changed.wait(), timeout=5)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            cancel.set()
            record = self.store.webhook_step(run_id, index)
            if record is not None and record["state"] not in {"succeeded", "failed", "ambiguous"}:
                if record["result"].get("status") in {None, "recording"}:
                    (self.recorder.directory / f"{run_id}.mp4").unlink(missing_ok=True)
                    self.store.finish_webhook_step(run_id, index, "failed", {}, "recording_cancelled")
            raise
        except Exception as error:
            reason = str(error) if isinstance(error, RecorderError) else type(error).__name__
            self.store.finish_webhook_step(run_id, index, "failed", {}, reason)
            log_event(logger, logging.WARNING, "webhook_video_failed", run_id=run_id,
                      error=reason)
        finally:
            self.changed.set()

    async def _review_video(self, run_id: str, index: int, step: dict[str, Any]) -> None:
        """Hand the finished recording to the agent, which decides what to send."""
        if not self.store.begin_webhook_video_review(run_id, index):
            return
        result = self.store.webhook_step(run_id, index)["result"]
        turn_id = f"webhook:{run_id}:{index}"
        if result.get("record_error"):
            prompt = (f"[系统通知] 摄像头视频录制失败（{result['record_error']}），"
                      "没有可以查看或发送的视频。")
        else:
            attachment = {"segments": [{"type": step.get("send_as", "file"), "data": {
                "file": result["path"], "name": Path(str(result["path"])).name,
            }}]}
            prompt = (
                f"[系统通知] 摄像头视频录制完成，时长约 "
                f"{float(result.get('duration_seconds') or 0):.0f} 秒，文件：{result['path']}。"
                "是否查看画面、是否回复、是否发送视频由你决定；"
                "发送视频时，在 reply 的 attachments 中放入 "
                f"{json.dumps(attachment, ensure_ascii=False)}。"
            )
        self.store.record_webhook_event(run_id, turn_id, prompt)
        try:
            with log_context(stage="webhook", run_id=run_id, step_index=index, turn_id=turn_id):
                reply = await self.complete_turn(prompt, turn_id)
            outbox_ids = self.store.commit_webhook_reply(
                run_id, index, turn_id, reply, self.primary_channel,
            )
        finally:
            if self.turn_settled is not None:
                self.turn_settled(turn_id)
        if outbox_ids:
            self.wake_outbox()
        elif result.get("record_error"):
            self.store.finish_webhook_step(run_id, index, "failed", {}, result["record_error"])

    async def _run_worker(self, stop: asyncio.Event) -> None:
        cleanup_at = monotonic() + 3600
        while not stop.is_set():
            if monotonic() >= cleanup_at:
                self.store.cleanup_webhook_video_assets(
                    self.recorder.directory, self.config.video_retention_days,
                    self.config.video_failed_retention_days,
                )
                cleanup_at = monotonic() + 3600
            run = self.store.claim_webhook_run()
            if run is None:
                self.changed.clear()
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=1)
                except TimeoutError:
                    pass
                continue
            try:
                self._active_run = asyncio.create_task(self._execute(run, stop))
                await self._active_run
            except asyncio.CancelledError:
                if not self._owner_stop_requested:
                    raise
                self.store.fail_webhook_run(str(run["id"]), "owner_stop")
                log_event(logger, logging.INFO, "webhook_run_stopped",
                          run_id=run["id"], reason="owner_stop")
            except Exception as error:
                log_event(
                    logger,
                    logging.ERROR,
                    "webhook_run_failure",
                    workflow_id=run["workflow_id"],
                    run_id=run["id"],
                    error_type=type(error).__name__,
                    exc_info=True,
                )
                self.store.fail_webhook_run(str(run["id"]), type(error).__name__)
            finally:
                self._active_run = None
                self._owner_stop_requested = False

    async def _execute(self, run: dict[str, Any], stop: asyncio.Event) -> None:
        try:
            await self._execute_steps(run, stop)
        finally:
            self._video_changed.set()

    async def _execute_steps(self, run: dict[str, Any], stop: asyncio.Event) -> None:
        run_id = str(run["id"])
        workflow_id = str(run["workflow_id"])
        steps = run["plan"]["steps"]
        video = next((step for step in steps if step["uses"] == "video"), None)
        for index in range(int(run["current_step"]), len(steps)):
            step = steps[index]
            step_started = monotonic()
            record = self.store.webhook_step(run_id, index)
            if record is None:
                raise RuntimeError("webhook step missing")
            if record["state"] in {"succeeded", "failed", "ambiguous"}:
                continue
            if step["uses"] == "video":
                continue
            if step["uses"] == "message":
                if record["state"] != "waiting_delivery":
                    self.store.start_webhook_step(run_id, index)
                    turn_id = f"webhook:{run_id}:{index}"
                    prompt = str(step["prompt"])
                    if video is not None and video.get("deliver", "auto") == "agent":
                        prompt += (f"\n\n[系统上下文] 摄像头视频（约 {video['duration_seconds']} 秒）"
                                   "正在后台录制。录制结束后你会再收到一条系统通知，届时可以查看画面，"
                                   "并自行决定是否发送视频。现在先说点什么，还是等看完视频再一起说，"
                                   "由你判断。")
                    elif video is not None:
                        prompt += (f"\n\n[系统上下文] 摄像头视频（约 {video['duration_seconds']} 秒）"
                                   "正在后台录制，完成后会单独发送，不需要你再发送或承诺具体时间。")
                    self.store.record_webhook_event(run_id, turn_id, prompt)
                    log_event(
                        logger,
                        logging.DEBUG,
                        "webhook_step_start",
                        stage="webhook",
                        workflow_id=workflow_id,
                        run_id=run_id,
                        step_index=index,
                        step_id=step["id"],
                        turn_id=turn_id,
                        step_kind="message",
                    )
                    try:
                        with log_context(
                            stage="webhook",
                            workflow_id=workflow_id,
                            run_id=run_id,
                            step_index=index,
                            turn_id=turn_id,
                        ):
                            reply = await self.complete_turn(prompt, turn_id)
                        outbox_ids = self.store.commit_webhook_reply(
                            run_id, index, turn_id, reply, self.primary_channel,
                        )
                    except Exception as error:
                        if video is None:
                            raise
                        self.store.finish_webhook_step(
                            run_id, index, "failed", {}, type(error).__name__
                        )
                        continue
                    finally:
                        if self.turn_settled is not None:
                            self.turn_settled(turn_id)
                    if not outbox_ids:
                        continue
                    self.wake_outbox()
                if not await self._wait_delivery(run_id, index, stop):
                    if video is None:
                        return
            else:
                self.store.start_webhook_step(run_id, index)
                log_event(
                    logger,
                    logging.DEBUG,
                    "webhook_step_start",
                    stage="webhook",
                    workflow_id=workflow_id,
                    run_id=run_id,
                    step_index=index,
                    step_id=step["id"],
                    step_kind="exec",
                    executor_id=step["executor"],
                )
                state, result, error = await self._run_exec(step)
                self.store.finish_webhook_step(run_id, index, state, result, error)
                log_event(
                    logger,
                    logging.DEBUG,
                    "webhook_step_end",
                    stage="webhook",
                    workflow_id=workflow_id,
                    run_id=run_id,
                    step_index=index,
                    step_id=step["id"],
                    executor_id=step["executor"],
                    state=state,
                    ok=state == "succeeded",
                    error=error,
                    duration_ms=int((monotonic() - step_started) * 1000),
                )
                if state != "succeeded":
                    return
        self.store.complete_webhook_run(run_id)
        settled = self.store.webhook_run(run_id)
        if settled is None or settled["state"] not in {"succeeded", "failed", "ambiguous"}:
            return
        log_event(
            logger,
            logging.INFO,
            "webhook_run_complete",
            stage="webhook",
            workflow_id=workflow_id,
            run_id=run_id,
            state=settled["state"],
            steps=len(steps),
        )

    async def _wait_delivery(
        self, run_id: str, step_index: int, stop: asyncio.Event
    ) -> bool:
        while not stop.is_set():
            state = self.store.webhook_delivery_state(run_id, step_index)
            if state == "succeeded":
                self.store.finish_webhook_step(
                    run_id, step_index, "succeeded", {}, None
                )
                log_event(
                    logger,
                    logging.DEBUG,
                    "webhook_delivery_end",
                    stage="webhook",
                    run_id=run_id,
                    step_index=step_index,
                    state=state,
                    ok=True,
                )
                return True
            if state in {"failed", "ambiguous"}:
                self.store.finish_webhook_step(
                    run_id, step_index, state, {}, f"message_delivery_{state}"
                )
                log_event(
                    logger,
                    logging.WARNING,
                    "webhook_delivery_end",
                    stage="webhook",
                    run_id=run_id,
                    step_index=step_index,
                    state=state,
                    ok=False,
                    reason="message_delivery_failed",
                )
                return False
            await asyncio.sleep(0.5)
        raise asyncio.CancelledError

    async def _run_exec(
        self, step: dict[str, Any]
    ) -> tuple[str, dict[str, Any], str | None]:
        try:
            result = await run_process(
                step["argv"], env={**os.environ, **step["env"]},
                timeout=float(step["timeout_seconds"]),
            )
        except TimeoutError:
            return "ambiguous", {}, "executor_timeout"
        except OSError as error:
            return "failed", {}, type(error).__name__
        if result["exit_code"] == 0:
            return "succeeded", result, None
        return "failed", result, f"executor_exit_{result['exit_code']}"
