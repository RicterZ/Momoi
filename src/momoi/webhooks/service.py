import asyncio
import hmac
import json
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

from aiohttp import web

from .catalog import WorkflowError, bind_workflow, load_catalog
from ..config import WebhookConfig
from ..logging_context import log_context, log_event
from ..models import AgentReply
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
    ) -> None:
        if config.workflows is None or config.executors is None:
            raise WorkflowError("webhook paths are not configured")
        self.config = config
        self.channel_variables = channel_variables
        self.store = store
        self.complete_turn = complete_turn
        self.wake_outbox = wake_outbox
        self.primary_channel = primary_channel
        self.workflows, self.executors = load_catalog(
            config.workflows, config.executors, set(channel_variables)
        )
        self.changed = asyncio.Event()

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
        while not stop.is_set():
            run = self.store.claim_webhook_run()
            if run is None:
                self.changed.clear()
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=1)
                except TimeoutError:
                    pass
                continue
            try:
                await self._execute(run, stop)
            except asyncio.CancelledError:
                raise
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

    async def _execute(self, run: dict[str, Any], stop: asyncio.Event) -> None:
        run_id = str(run["id"])
        workflow_id = str(run["workflow_id"])
        steps = run["plan"]["steps"]
        for index in range(int(run["current_step"]), len(steps)):
            step = steps[index]
            step_started = monotonic()
            record = self.store.webhook_step(run_id, index)
            if record is None:
                raise RuntimeError("webhook step missing")
            if record["state"] == "succeeded":
                continue
            if step["uses"] == "message":
                if record["state"] != "waiting_delivery":
                    self.store.start_webhook_step(run_id, index)
                    turn_id = f"webhook:{run_id}:{index}"
                    self.store.record_webhook_event(
                        run_id, turn_id, str(step["prompt"])
                    )
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
                    with log_context(
                        stage="webhook",
                        workflow_id=workflow_id,
                        run_id=run_id,
                        step_index=index,
                        turn_id=turn_id,
                    ):
                        reply = await self.complete_turn(str(step["prompt"]), turn_id)
                    outbox_ids = self.store.commit_webhook_reply(
                        run_id,
                        index,
                        turn_id,
                        reply,
                        self.primary_channel,
                    )
                    if not outbox_ids:
                        continue
                    self.wake_outbox()
                if not await self._wait_delivery(run_id, index, stop):
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
        log_event(
            logger,
            logging.INFO,
            "webhook_run_complete",
            stage="webhook",
            workflow_id=workflow_id,
            run_id=run_id,
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
            if state == "failed":
                self.store.finish_webhook_step(
                    run_id, step_index, "failed", {}, "message_delivery_failed"
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
        env = {**os.environ, **step["env"]}
        try:
            process = await asyncio.create_subprocess_exec(
                *step["argv"],
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            return "failed", {}, type(error).__name__
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=float(step["timeout_seconds"])
            )
        except TimeoutError:
            await self._terminate(process)
            return "ambiguous", {}, "executor_timeout"
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        result = {
            "exit_code": process.returncode,
            "stdout_tail": stdout[-16384:].decode(errors="replace"),
            "stderr_tail": stderr[-16384:].decode(errors="replace"),
        }
        if process.returncode == 0:
            return "succeeded", result, None
        return "failed", result, f"executor_exit_{process.returncode}"

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=5)
        except (ProcessLookupError, TimeoutError):
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
