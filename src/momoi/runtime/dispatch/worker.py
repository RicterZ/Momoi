import asyncio
import logging
from typing import Any

from ...observability.events import log_event
from ...models import AgentReply, IncomingMessage
from ..jobs import AutonomousJob

logger = logging.getLogger("momoi.runtime.daemon")


class AgentWorker:
    async def _agent_worker(self, stop: asyncio.Event) -> None:
        batch: list[IncomingMessage] = []
        quiet_deadline = 0.0
        hard_deadline = 0.0
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            if not batch:
                kind, item = await self._next_work()
                if kind == "webhook":
                    prompt, turn_id, future = item
                    if future.cancelled():
                        continue
                    self._webhook_turn_active = True
                    try:
                        reply = await self._complete_webhook_turn(
                            prompt, turn_id, self.channel
                        )
                    except asyncio.CancelledError:
                        if not future.done():
                            future.cancel()
                        raise
                    except Exception as error:
                        if not future.done():
                            future.set_exception(error)
                    else:
                        if not future.done():
                            future.set_result(reply)
                    finally:
                        self._webhook_turn_active = False
                    continue
                if kind == "goal":
                    job = item
                    assert isinstance(job, AutonomousJob)
                    self._stop_requested = False
                    requeue_memory_maintenance = False
                    if job.kind == "heartbeat":
                        target_channel = self._manual_heartbeat_channel
                        self._manual_heartbeat_channel = None
                        work = self._complete_heartbeat_turn(stop, target_channel)
                    elif job.kind == "reflection":
                        work = self._complete_reflection_turn(job.id, stop)
                    elif job.kind == "memory_maintenance":
                        work = self._complete_memory_maintenance_turn(job.id, stop)
                    else:
                        work = self._complete_goal_turn(job.id, stop)
                    self._active_turn = asyncio.create_task(work)
                    try:
                        result = await self._active_turn
                        requeue_memory_maintenance = (
                            job.kind == "memory_maintenance" and result is True
                        )
                    except asyncio.CancelledError:
                        if not self._stop_requested:
                            raise
                        if job.kind == "heartbeat":
                            self.store.release_heartbeat_claim(
                                self._heartbeat_retry_delay()
                            )
                            log_event(
                                logger,
                                logging.INFO,
                                "turn_cancelled",
                                stage="heartbeat",
                                reason="owner_stop",
                            )
                        elif job.kind == "reflection":
                            local_date = job.id
                            self.store.release_reflection(
                                local_date, "owner_stop", delay_seconds=3600
                            )
                            log_event(
                                logger,
                                logging.INFO,
                                "turn_cancelled",
                                stage="reflection",
                                local_date=local_date,
                                reason="owner_stop",
                            )
                        elif job.kind == "memory_maintenance":
                            log_event(
                                logger,
                                logging.INFO,
                                "turn_cancelled",
                                stage="memory_maintenance",
                                turn_id=job.id,
                                reason="owner_stop",
                            )
                        else:
                            self.store.release_goal_claim(job.id, defer_seconds=900)
                            log_event(
                                logger,
                                logging.INFO,
                                "turn_cancelled",
                                stage="goal",
                                goal_id=job.id,
                                reason="owner_stop",
                            )
                    finally:
                        if job.kind == "memory_maintenance":
                            self._queued_memory_maintenance.discard(job.id)
                            if requeue_memory_maintenance and not stop.is_set():
                                self._enqueue_memory_maintenance(job.id)
                        self._active_turn = None
                        self._stop_requested = False
                        self.agenda_changed.set()
                    continue
                message = item
                assert isinstance(message, IncomingMessage)
                batch.append(message)
                channel = self._channel_for(message.channel)
                now = loop.time()
                immediate = message.text.strip() == "/stop"
                quiet_deadline = now if immediate else now + channel.quiet_seconds
                hard_deadline = now if immediate else now + channel.max_batch_seconds
                continue
            channel = self._channel_for(batch[0].channel)
            quiet_deadline = min(
                max(
                    quiet_deadline,
                    self._owner_quiet_until.get(channel.name, 0.0),
                ),
                hard_deadline,
            )
            timeout = max(0.0, quiet_deadline - loop.time())
            try:
                if self.incoming.empty():
                    self._owner_activity_changed.clear()
                    if self.incoming.empty():
                        await asyncio.wait_for(
                            self._owner_activity_changed.wait(), timeout=timeout
                        )
                        continue
                message = self.incoming.get_nowait()
                if message.text.strip() == "/stop":
                    self.store.discard_events(batch)
                    batch = [message]
                    quiet_deadline = loop.time()
                    hard_deadline = quiet_deadline
                    continue
                if message.channel != batch[0].channel:
                    self._deferred_incoming.append(message)
                    quiet_deadline = loop.time()
                    hard_deadline = quiet_deadline
                    continue
                batch.append(message)
                channel = self._channel_for(message.channel)
                quiet_deadline = min(loop.time() + channel.quiet_seconds, hard_deadline)
            except TimeoutError:
                sealed = batch
                batch = []
                self._stop_requested = False
                sealed_turn_id = self._turn_id(*(event.event_id for event in sealed))
                self._active_turn = asyncio.create_task(
                    self._complete_batch_turn(
                        sealed,
                        stop,
                        sealed_turn_id,
                        self._channel_for(sealed[0].channel),
                    )
                )
                try:
                    await self._active_turn
                except asyncio.CancelledError:
                    if not self._stop_requested:
                        raise
                    self.store.cancel_turn(sealed_turn_id, sealed)
                    log_event(
                        logger,
                        logging.INFO,
                        "turn_cancelled",
                        stage="owner",
                        turn_id=sealed_turn_id,
                        channel=sealed[0].channel,
                        reason="owner_stop",
                    )
                finally:
                    self._active_turn = None
                    self._stop_requested = False

    async def _next_work(self) -> tuple[str, Any]:
        queued: list[IncomingMessage] = []
        while not self.incoming.empty():
            queued.append(self.incoming.get_nowait())
        stopped = next((item for item in queued if item.text.strip() == "/stop"), None)
        self._deferred_incoming.extend(item for item in queued if item is not stopped)
        if stopped is not None:
            return "owner", stopped
        if self._deferred_incoming:
            return "owner", self._deferred_incoming.popleft()
        if not self.webhook_requests.empty():
            return "webhook", await self.webhook_requests.get()
        if not self.autonomous.empty():
            return "goal", self._next_autonomous()
        owner = asyncio.create_task(self.incoming.get())
        webhook = asyncio.create_task(self.webhook_requests.get())
        goal = asyncio.create_task(self.autonomous.get())
        tasks = {
            "owner": (owner, self.incoming),
            "webhook": (webhook, self.webhook_requests),
            "goal": (goal, self.autonomous),
        }
        try:
            done, _ = await asyncio.wait(
                {owner, webhook, goal}, return_when=asyncio.FIRST_COMPLETED
            )
            chosen_kind = next(
                kind for kind in ("owner", "webhook", "goal") if tasks[kind][0] in done
            )
            chosen = tasks[chosen_kind][0]
            for kind, (task, queue) in tasks.items():
                if kind == chosen_kind:
                    continue
                if task.done() and not task.cancelled():
                    queue.put_nowait(task.result())
                else:
                    task.cancel()
            item = chosen.result()
            return (
                chosen_kind,
                self._prioritize_autonomous(item) if chosen_kind == "goal" else item,
            )
        except BaseException:
            for task, _ in tasks.values():
                if not task.done():
                    task.cancel()
            raise

    def _next_autonomous(self) -> AutonomousJob:
        return self._prioritize_autonomous(self.autonomous.get_nowait())

    def _enqueue_memory_maintenance(self, turn_id: str) -> None:
        if turn_id in self._queued_memory_maintenance:
            return
        self._queued_memory_maintenance.add(turn_id)
        self.autonomous.put_nowait(AutonomousJob.memory_maintenance(turn_id))

    def _prioritize_autonomous(self, current: AutonomousJob) -> AutonomousJob:
        candidates = [current]
        while not self.autonomous.empty():
            candidates.append(self.autonomous.get_nowait())
        selected_index = min(
            range(len(candidates)),
            key=lambda index: (candidates[index].effective_priority, index),
        )
        selected = candidates.pop(selected_index)
        for item in candidates:
            self.autonomous.put_nowait(item.waited())
        return selected

    async def _request_webhook_turn(self, prompt: str, turn_id: str) -> AgentReply:
        future: asyncio.Future[AgentReply] = asyncio.get_running_loop().create_future()
        await self.webhook_requests.put((prompt, turn_id, future))
        return await future
