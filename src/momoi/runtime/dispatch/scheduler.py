import asyncio
import logging
from time import time

from ...observability.events import log_event
from ...observability.values import safe_preview
from ...storage import (
    CURRENT_STATE_BATCH_SIZE,
    CURRENT_STATE_FULL_IDLE_SECONDS,
    CURRENT_STATE_PARTIAL_IDLE_SECONDS,
    EPISODE_CONSOLIDATION_BATCH_SIZE,
    EPISODE_CONSOLIDATION_DEFER_TIMEOUT_SECONDS,
    EPISODE_CONSOLIDATION_PARTIAL_IDLE_SECONDS,
)
from ..jobs import AutonomousJob

logger = logging.getLogger("momoi.runtime.daemon")
AGENDA_POLL_SECONDS = 5


class Scheduler:
    def _maintenance_is_idle(self, *, autonomous_queue_empty: bool = False) -> bool:
        active = self._active_turn
        if active is not None and not active.done():
            return False
        if self._webhook_turn_active:
            return False
        if (
            not self.incoming.empty()
            or self._deferred_incoming
            or not self.webhook_requests.empty()
        ):
            return False
        if autonomous_queue_empty and not self.autonomous.empty():
            return False
        return not bool(self.store.heartbeat_conversation_snapshot()["owner_busy"])

    def _idle_quiet_seconds(self) -> float:
        return asyncio.get_running_loop().time() - self._last_owner_activity_at

    @staticmethod
    def _batch_idle_seconds(
        pending_count: int,
        *,
        batch_size: int,
        full_idle_seconds: float,
        partial_idle_seconds: float,
    ) -> float:
        """A partial batch runs after the short idle; anything else waits in full."""

        partial = 0 < pending_count < batch_size
        return partial_idle_seconds if partial else full_idle_seconds

    def _current_state_batch_ready(self) -> bool:
        status = self.store.current_state_batch_status()
        if status is None or status["retry_at"] > time():
            return False
        idle_seconds = self._batch_idle_seconds(
            status["count"],
            batch_size=CURRENT_STATE_BATCH_SIZE,
            full_idle_seconds=CURRENT_STATE_FULL_IDLE_SECONDS,
            partial_idle_seconds=CURRENT_STATE_PARTIAL_IDLE_SECONDS,
        )
        return self._maintenance_is_idle() and self._idle_quiet_seconds() >= idle_seconds

    def _episode_annealing_ready(self) -> bool:
        if not self.config.episode_annealing.enabled:
            return False
        active = self._active_annealing
        if active is not None and not active.done():
            return False
        retry_at = self.store.next_episode_annealing_retry_at()
        retry_due = retry_at is not None and retry_at <= time()
        if not self._episode_annealing_dirty and not retry_due:
            return False
        idle_seconds = self._batch_idle_seconds(
            self.store.episode_consolidation_pending_count(),
            batch_size=EPISODE_CONSOLIDATION_BATCH_SIZE,
            full_idle_seconds=self.config.episode_annealing.idle_seconds,
            partial_idle_seconds=EPISODE_CONSOLIDATION_PARTIAL_IDLE_SECONDS,
        )
        return (
            self._maintenance_is_idle(autonomous_queue_empty=True)
            and self._idle_quiet_seconds() >= idle_seconds
        )

    def _episode_consolidation_minimum(self) -> int:
        pending = self.store.episode_consolidation_pending_count()
        return (
            1 if 0 < pending < EPISODE_CONSOLIDATION_BATCH_SIZE
            else EPISODE_CONSOLIDATION_BATCH_SIZE
        )

    def _maybe_start_episode_annealing(self) -> None:
        if not self._episode_annealing_ready():
            return
        self._episode_annealing_dirty = False
        task = asyncio.create_task(
            self._run_episode_annealing_once(
                consolidation_minimum=self._episode_consolidation_minimum()
            )
        )
        self._active_annealing = task
        task.add_done_callback(self._episode_annealing_finished)

    def _episode_annealing_finished(self, task: asyncio.Task[bool]) -> None:
        self._active_annealing = None
        if task.cancelled():
            log_event(
                logger,
                logging.DEBUG,
                "episode_anneal_cancelled",
                stage="episode_anneal",
                reason="preempted",
            )
            self._episode_annealing_dirty = True
        elif (error := task.exception()) is not None:
            log_event(
                logger,
                logging.WARNING,
                "episode_anneal_failure",
                stage="episode_anneal",
                error_type=type(error).__name__,
                reason=safe_preview(str(error), 300),
            )
            # The failed Episode remains protected by its persisted retry
            # deadline; re-evaluate so other eligible work can proceed.
            self._episode_annealing_dirty = True
        elif task.result():
            self._episode_annealing_dirty = True
        self.agenda_changed.set()

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
                self._episode_annealing_dirty = True
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
            plan = self.store.claim_task_plan()
            if plan is not None:
                await self.autonomous.put(AutonomousJob("plan_step", plan["id"]))
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
            state = self.store.current_state_batch_status()
            state_id = str(state["source_turn_id"]) if state else None
            if (
                state_id is not None
                and self._current_state_batch_ready()
                and state_id not in self._queued_current_state
            ):
                self._queued_current_state.add(state_id)
                self.autonomous.put_nowait(AutonomousJob.current_state(state_id))
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
            self._maybe_start_episode_annealing()
            due_times = [
                due
                for due in (
                    self.store.next_notification_due_at(),
                    self.store.next_goal_due_at(),
                    self.store.next_memory_operation_due_at(),
                    self.store.next_episode_annealing_retry_at(),
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
