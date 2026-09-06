import asyncio
import logging
from time import time

from ...observability.events import log_event
from ...observability.values import safe_preview
from ...storage import (
    EPISODE_CONSOLIDATION_DEFER_TIMEOUT_SECONDS,
)
from ..jobs import AutonomousJob

logger = logging.getLogger("momoi.runtime.daemon")
AGENDA_POLL_SECONDS = 5


class Scheduler:
    def _episode_annealing_is_idle(self) -> bool:
        active = self._active_turn
        if active is not None and not active.done():
            return False
        if self._webhook_turn_active:
            return False
        if (
            not self.incoming.empty()
            or self._deferred_incoming
            or not self.webhook_requests.empty()
            or not self.autonomous.empty()
        ):
            return False
        return not bool(self.store.heartbeat_conversation_snapshot()["owner_busy"])

    async def _wait_for_episode_annealing_ready(
        self, stop: asyncio.Event
    ) -> bool | None:
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            quiet_for = loop.time() - self._last_owner_activity_at
            if (
                self._episode_annealing_is_idle()
                and quiet_for >= self.config.episode_annealing.idle_seconds
            ):
                return True
            remaining = max(
                0.05,
                self.config.episode_annealing.idle_seconds - quiet_for,
            )
            try:
                await asyncio.wait_for(
                    self.episode_annealing_requested.wait(),
                    timeout=min(1.0, remaining),
                )
            except TimeoutError:
                pass
            else:
                self.episode_annealing_requested.clear()
        return None

    async def _wait_for_episode_annealing_retry(self) -> None:
        retry_at = self.store.next_episode_annealing_retry_at()
        if retry_at is None:
            return
        delay = max(1.0, retry_at - time())
        try:
            await asyncio.wait_for(
                self.episode_annealing_requested.wait(),
                timeout=delay,
            )
        except TimeoutError:
            self.episode_annealing_requested.set()

    async def _episode_annealing_worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.episode_annealing_requested.wait()
            self.episode_annealing_requested.clear()
            if not self.config.episode_annealing.enabled:
                continue
            ready = await self._wait_for_episode_annealing_ready(stop)
            if ready is None:
                return
            task = asyncio.create_task(self._run_episode_annealing_once())
            self._active_annealing = task
            try:
                completed = await task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if stop.is_set() or (current is not None and current.cancelling()):
                    raise
                log_event(
                    logger,
                    logging.DEBUG,
                    "episode_anneal_cancelled",
                    stage="episode_anneal",
                    reason="owner_update",
                )
                self.episode_annealing_requested.set()
            except Exception as error:
                log_event(
                    logger,
                    logging.WARNING,
                    "episode_anneal_failure",
                    stage="episode_anneal",
                    error_type=type(error).__name__,
                    reason=safe_preview(str(error), 300),
                )
                # The failed Episode remains protected by its persisted retry
                # deadline; wake the worker so other eligible work can proceed.
                self.episode_annealing_requested.set()
            else:
                if completed:
                    self.episode_annealing_requested.set()
                else:
                    await self._wait_for_episode_annealing_retry()
            finally:
                self._active_annealing = None

    async def _scheduler_worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.agenda_changed.clear()
            expired_deferrals = (
                self.store.cleanup_expired_episode_consolidation_deferrals()
            )
            if expired_deferrals:
                log_event(
                    logger,
                    logging.INFO,
                    "episode_deferred_cleanup",
                    stage="scheduler",
                    ignored=expired_deferrals,
                    timeout_seconds=EPISODE_CONSOLIDATION_DEFER_TIMEOUT_SECONDS,
                )
                self.episode_annealing_requested.set()
            notification = self.store.claim_due_notification(self.config.notifications)
            if notification is not None:
                if self.store.queue_notification(
                    str(notification["id"]),
                    config=self.config.notifications,
                    primary_channel=self.channel.name,
                ):
                    log_event(
                        logger,
                        logging.INFO,
                        "notification_queued",
                        notification_id=notification["id"],
                    )
                    self.outbox_changed.set()
                continue
            goal = self.store.claim_due_goal()
            if goal is not None:
                log_event(
                    logger,
                    logging.INFO,
                    "goal_queued",
                    stage="scheduler",
                    goal_id=goal["id"],
                    title=goal["title"],
                    next_review_at=goal.get("next_review_timestamp"),
                    schedule=goal.get("schedule"),
                )
                await self.autonomous.put(AutonomousJob.goal(str(goal["id"])))
                continue
            operation_id = self.store.pending_memory_operation()
            if operation_id is not None and operation_id not in self._queued_memory_operations:
                self._enqueue_memory_operation(operation_id)
                continue
            reflection = self.store.claim_due_reflection(self.config.reflection)
            if reflection is not None:
                log_event(
                    logger,
                    logging.INFO,
                    "reflection_queued",
                    stage="scheduler",
                    local_date=reflection["local_date"],
                )
                await self.autonomous.put(
                    AutonomousJob.reflection(str(reflection["local_date"]))
                )
                continue
            maintenance_turn_id = self.store.pending_memory_maintenance_turn()
            if (
                maintenance_turn_id is not None
                and maintenance_turn_id not in self._queued_memory_maintenance
            ):
                self._enqueue_memory_maintenance(maintenance_turn_id)
                continue
            heartbeat = self.store.claim_due_heartbeat(
                self.config.heartbeat, self.config.notifications
            )
            if heartbeat is not None:
                log_event(
                    logger,
                    logging.DEBUG,
                    "heartbeat_queued",
                    stage="scheduler",
                )
                await self.autonomous.put(AutonomousJob.heartbeat())
                continue
            due_times = [
                due
                for due in (
                    self.store.next_notification_due_at(),
                    self.store.next_goal_due_at(),
                    self.store.next_memory_operation_due_at(),
                    self.store.next_reflection_due_at(
                        self.config.reflection,
                    ),
                    self.store.next_heartbeat_due_at(self.config.heartbeat.enabled),
                )
                if due is not None
            ]
            if not due_times:
                try:
                    await asyncio.wait_for(
                        self.agenda_changed.wait(), timeout=AGENDA_POLL_SECONDS
                    )
                except TimeoutError:
                    pass
                continue
            due_at = min(due_times)
            timeout = min(
                AGENDA_POLL_SECONDS,
                max(0.0, due_at - time()),
            )
            try:
                await asyncio.wait_for(self.agenda_changed.wait(), timeout=timeout)
            except TimeoutError:
                pass
