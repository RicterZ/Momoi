import asyncio
import logging
import random
from collections import deque
from time import monotonic, time
from typing import Any

from ..agenda_tools import AgendaTools
from ..asr import ASRProvider, load_asr_provider
from ..builtin_tools import BuiltinTools
from ..channel import (
    AmbiguousSend,
    Channel,
    ChannelDependencies,
    NotConnected,
    SendRejected,
    create_channel,
)
from ..config import AppConfig
from ..policies import DaemonPolicy
from ..dashboard import DashboardService
from ..extensions import load_usage_plugin
from ..logging_context import TRACE, log_event, safe_preview
from ..memory_tools import MemoryTools
from ..mcp_client import MCPManager
from ..models import AgentReply, IncomingMessage, OwnerInputStatus
from ..provider import AnthropicProvider, OpenAIProvider
from ..semantic import SemanticRecallService
from ..storage import Store
from ..webhooks import WebhookService
from .jobs import AutonomousJob
from .memory_maintenance import MEMORY_MAINTENANCE_RUN_VERSION
from .tool_result_store import ToolResultStore
from .turns import TurnRunner

logger = logging.getLogger(__name__)
AGENDA_POLL_SECONDS = 5
_DEFAULT_DAEMON_POLICY = DaemonPolicy()


def _message_gap_bounds(
    text: str, policy: DaemonPolicy = _DEFAULT_DAEMON_POLICY
) -> tuple[float, float]:
    ratio = min(
        1.0,
        max(
            0.0,
            (len(text.strip()) - policy.message_gap_min_chars)
            / (policy.message_gap_saturation_chars - policy.message_gap_min_chars),
        ),
    )
    lower = policy.message_gap_min_seconds + 2 * ratio
    upper = lower + 1
    return lower, min(policy.message_gap_max_seconds, upper)


class MomoiDaemon(TurnRunner):
    def __init__(
        self,
        config: AppConfig,
        channel: Channel | None = None,
        dashboard: tuple[str, int] | None = None,
        asr_provider: ASRProvider | None = None,
    ) -> None:
        self.config = config
        self._loaded_workspace_prompts: dict[str, str] = {}
        self.daemon_policy = config.policies.daemon
        self._artifact_root().mkdir(parents=True, exist_ok=True)
        self.tool_results = ToolResultStore(
            self._tool_result_root(),
            retention_days=config.tool_result_retention_days,
        )
        self.store = Store(
            config.database,
            config.workspace,
            config.policies.memory,
            thinking=config.thinking,
            timezone=config.timezone,
        )
        self.semantic_recall = SemanticRecallService(
            self.store,
            config.embedding,
            policy=config.policies.semantic,
        )
        self.semantic_recall.start()
        usage_plugin = None
        if config.usage.provider:
            usage_plugin = load_usage_plugin(
                config.usage.provider,
                api_key=config.usage.api_key,
                **(config.usage.settings or {}),
            )
            self.store.set_usage_plugin(usage_plugin)
            log_event(
                logger,
                logging.INFO,
                "usage_plugin_loaded",
                provider=config.usage.provider,
            )
        self.dashboard = (
            DashboardService(
                self.store,
                *dashboard,
                token=config.dashboard.token,
                usage_plugin=usage_plugin,
            )
            if dashboard is not None
            else None
        )
        self.store.ensure_heartbeat(config.heartbeat)
        self.agenda_tools = AgendaTools(self.store)
        self.memory_tools = MemoryTools(
            self.store, config.policies.memory, self.semantic_recall
        )
        self.builtin_tools = BuiltinTools(
            config.workspace or config.database.parent,
            private_roots=(self._tool_result_root(),),
        )
        self.asr_provider = asr_provider if config.asr.enabled else None
        if config.asr.enabled and self.asr_provider is None:
            settings = dict(config.asr.settings or {})
            settings.setdefault("timeout_seconds", config.asr.timeout_seconds)
            self.asr_provider = load_asr_provider(config.asr.provider, **settings)

        def build_channel(item: object) -> Channel:
            dependencies = (
                ChannelDependencies(
                    asr_provider=self.asr_provider,
                    asr_max_audio_bytes=config.asr.max_audio_bytes,
                )
                if getattr(item, "plugin", "") == "napcat"
                else None
            )
            return create_channel(item, dependencies)

        created = (
            (channel,)
            if channel is not None
            else tuple(build_channel(item) for item in config.channel_configs)
        )
        self.channels = {item.name: item for item in created}
        if len(self.channels) != len(created):
            raise ValueError("channel plugin names must be unique")
        if channel is not None:
            self.channel = channel
        else:
            primary_name = str(getattr(config.channel, "plugin", ""))
            self.channel = self.channels[primary_name]
        dump_dir = config.workspace / "llm-dumps" if config.workspace else None
        self.provider = (
            OpenAIProvider(config.llm, dump_dir)
            if config.llm.api_format == "openai"
            else AnthropicProvider(config.llm, dump_dir)
        )
        self.provider.usage_sink = self.store.record_llm_call
        self.provider.thinking_sink = self.store.record_thinking_call
        if usage_plugin is not None:
            self.provider.usage_parser = usage_plugin.parse_usage
        self.mcp = MCPManager(config.mcp_config)
        self.incoming: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        self._deferred_incoming: deque[IncomingMessage] = deque()
        self._owner_quiet_until: dict[str, float] = {}
        self._last_owner_activity_at = 0.0
        self._owner_activity_changed = asyncio.Event()
        self._owner_message_changed = asyncio.Event()
        self.webhook_requests: asyncio.Queue[
            tuple[str, str, asyncio.Future[AgentReply]]
        ] = asyncio.Queue()
        self.autonomous: asyncio.Queue[AutonomousJob] = asyncio.Queue()
        self.episode_annealing_requested = asyncio.Event()
        self.outbox_changed = asyncio.Event()
        self.agenda_changed = asyncio.Event()
        self._active_turn: asyncio.Task[Any] | None = None
        self._active_annealing: asyncio.Task[None] | None = None
        self._webhook_turn_active = False
        self._stop_requested = False
        self._manual_heartbeat_channel: str | None = None
        self._queued_memory_maintenance: set[str] = set()
        self.webhooks = (
            WebhookService(
                config.webhooks,
                self._workflow_variables(),
                self.store,
                self._request_webhook_turn,
                self.outbox_changed.set,
                self.channel.name,
            )
            if config.webhooks.enabled
            else None
        )

    async def run(self, stop: asyncio.Event) -> None:
        if self.config.episode_annealing.enabled:
            self.episode_annealing_requested.set()
        for turn_id in self.store.recover_memory_maintenance_turns():
            self._enqueue_memory_maintenance(turn_id)
        for event in self.store.pending_events():
            self.incoming.put_nowait(event)
        async with self.mcp, self.provider:
            tasks: list[asyncio.Task[None]] = []
            try:
                async with asyncio.TaskGroup() as group:
                    tasks.extend(
                        group.create_task(self._run_channel(item, stop))
                        for item in self.channels.values()
                    )
                    tasks.append(group.create_task(self._agent_worker(stop)))
                    tasks.append(group.create_task(self._scheduler_worker(stop)))
                    tasks.append(group.create_task(self._outbox_worker(stop)))
                    tasks.append(
                        group.create_task(self._episode_annealing_worker(stop))
                    )
                    tasks.append(
                        group.create_task(
                            self.semantic_recall.run_worker(
                                stop,
                                busy=lambda: (
                                    self._active_turn is not None
                                    or self._webhook_turn_active
                                    or self._active_annealing is not None
                                ),
                            )
                        )
                    )
                    if self.dashboard is not None:
                        tasks.append(group.create_task(self.dashboard.run(stop)))
                    if self.webhooks is not None:
                        tasks.append(group.create_task(self.webhooks.run_api(stop)))
                        tasks.append(group.create_task(self.webhooks.run_worker(stop)))
                    await stop.wait()
                    for task in tasks:
                        task.cancel()
            finally:
                await self.semantic_recall.close()
                self.store.close()

    async def _run_channel(self, channel: Channel, stop: asyncio.Event) -> None:
        log_event(logger, logging.INFO, "channel_start", channel=channel.name)
        try:
            await channel.run(self._receive, stop)
            if not stop.is_set():
                log_event(
                    logger,
                    logging.ERROR,
                    "channel_stop",
                    channel=channel.name,
                    reason="unexpected_return",
                )
                await stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_event(
                logger,
                logging.ERROR,
                "channel_stop",
                channel=channel.name,
                error_type=type(error).__name__,
                exc_info=True,
            )
            await stop.wait()

    def _channel_for(self, name: str) -> Channel:
        if name in self.channels:
            return self.channels[name]
        if name in {"", "unknown"}:
            return self.channel
        raise ValueError(f"message references an unconfigured channel: {name}")

    def _workflow_variables(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for channel in self.channels.values():
            values.update(
                {
                    key: value
                    for key, value in channel.workflow_variables().items()
                    if key not in {"owner_id", "channel_url"}
                }
            )
        values.update(self.channel.workflow_variables())
        return values

    def _touch_owner_activity(
        self, channel_name: str, *, quiet_extension: float | None = None
    ) -> None:
        channel = self._channel_for(channel_name)
        now = asyncio.get_running_loop().time()
        self._last_owner_activity_at = now
        extension = (
            channel.quiet_seconds if quiet_extension is None else quiet_extension
        )
        self._owner_quiet_until[channel.name] = max(
            self._owner_quiet_until.get(channel.name, 0.0),
            now + max(0.0, extension),
        )
        self._owner_activity_changed.set()

    async def _receive(self, event: IncomingMessage | OwnerInputStatus) -> None:
        if isinstance(event, OwnerInputStatus):
            channel = self._channel_for(event.channel)
            self._touch_owner_activity(
                event.channel,
                quiet_extension=channel.quiet_seconds / 2,
            )
            log_event(logger, TRACE, "owner_input_status", channel=event.channel)
            return
        message = event
        log_event(
            logger,
            logging.INFO,
            "owner_message_received",
            channel=message.channel,
            event_id=message.event_id,
            content=safe_preview(message.text, 500),
        )
        if message.text.strip() == "/stop":
            stop_channel = self._channel_for(message.channel).name
            cancelled_outbox = self.store.cancel_pending_outbox(
                stop_channel, "owner_stop"
            )
            if cancelled_outbox:
                self.outbox_changed.set()
            active = self._active_turn
            if active is not None and not active.done():
                self._stop_requested = True
                active.cancel()
            if self.store.add_event(message):
                log_event(
                    logger,
                    logging.INFO,
                    "owner_command_accepted",
                    channel=message.channel,
                    event_id=message.event_id,
                    command="stop",
                    cancelled_outbox=cancelled_outbox,
                )
                await self.incoming.put(message)
            return
        if message.text.strip() == "/heartbeat":
            if self.store.claim_manual_heartbeat():
                log_event(
                    logger,
                    logging.INFO,
                    "owner_command_accepted",
                    channel=message.channel,
                    event_id=message.event_id,
                    command="heartbeat",
                )
                self._manual_heartbeat_channel = message.channel
                await self.autonomous.put(AutonomousJob.heartbeat())
            else:
                log_event(
                    logger,
                    logging.INFO,
                    "owner_command_ignored",
                    channel=message.channel,
                    event_id=message.event_id,
                    command="heartbeat",
                    reason="heartbeat_already_active",
                )
            return
        if message.text.strip() == "/tidy":
            turn_id = self.store.pending_memory_maintenance_turn()
            if turn_id is None:
                turn_id = self._turn_id(
                    "memory-maintenance",
                    MEMORY_MAINTENANCE_RUN_VERSION,
                    "manual",
                    message.event_id,
                )
                self.store.queue_memory_maintenance_turn(
                    turn_id, f"manual:{message.event_id}"
                )
            annealing = self._active_annealing
            if annealing is not None and not annealing.done():
                annealing.cancel("memory_maintenance")
            self._enqueue_memory_maintenance(turn_id)
            log_event(
                logger,
                logging.INFO,
                "owner_command_accepted",
                channel=message.channel,
                event_id=message.event_id,
                command="tidy",
                turn_id=turn_id,
            )
            return
        if message.text.strip() == "/reflect":
            reflection = self.store.claim_manual_reflection()
            if reflection is not None:
                log_event(
                    logger,
                    logging.INFO,
                    "owner_command_accepted",
                    channel=message.channel,
                    event_id=message.event_id,
                    command="reflect",
                    local_date=reflection["local_date"],
                )
                await self.autonomous.put(
                    AutonomousJob.reflection(str(reflection["local_date"]))
                )
            else:
                log_event(
                    logger,
                    logging.INFO,
                    "owner_command_ignored",
                    channel=message.channel,
                    event_id=message.event_id,
                    command="reflect",
                    reason="reflection_already_active",
                )
            return
        if self.store.add_event(message):
            await self.incoming.put(message)
            annealing = self._active_annealing
            if annealing is not None and not annealing.done():
                annealing.cancel("owner_update")
            if self.config.episode_annealing.enabled:
                self.episode_annealing_requested.set()
            self._owner_message_changed.set()
            self._touch_owner_activity(message.channel)

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
                hard_deadline = (
                    now if immediate else now + channel.max_batch_seconds
                )
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
                quiet_deadline = min(
                    loop.time() + channel.quiet_seconds, hard_deadline
                )
            except TimeoutError:
                sealed = batch
                batch = []
                self._stop_requested = False
                sealed_turn_id = self._turn_id(
                    *(event.event_id for event in sealed)
                )
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
        stopped = next(
            (item for item in queued if item.text.strip() == "/stop"), None
        )
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
        self.autonomous.put_nowait(
            AutonomousJob.memory_maintenance(turn_id)
        )

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

    async def _request_webhook_turn(
        self, prompt: str, turn_id: str
    ) -> AgentReply:
        future: asyncio.Future[AgentReply] = asyncio.get_running_loop().create_future()
        await self.webhook_requests.put((prompt, turn_id, future))
        return await future

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
            if self._episode_annealing_is_idle():
                if self.store.episode_consolidation_pending_count() >= 6:
                    return False
                if quiet_for >= self.config.episode_annealing.idle_seconds:
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
            allow_partial = await self._wait_for_episode_annealing_ready(stop)
            if allow_partial is None:
                return
            task = asyncio.create_task(
                self._run_episode_annealing_once(
                    allow_partial_consolidation=allow_partial
                )
            )
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

    async def _outbox_worker(self, stop: asyncio.Event) -> None:
        previous_delivery: tuple[str, str] | None = None
        while not stop.is_set():
            self.outbox_changed.clear()
            rows = self.store.due_outbox()
            if not rows:
                try:
                    await asyncio.wait_for(self.outbox_changed.wait(), timeout=5)
                except TimeoutError:
                    pass
                continue
            for row in rows:
                try:
                    channel = self._channel_for(row.channel)
                except ValueError:
                    log_event(
                        logger,
                        logging.ERROR,
                        "outbox_failure",
                        stage="delivery",
                        turn_id=row.turn_id,
                        channel=row.channel,
                        outbox_id=row.id,
                        reason="channel_not_configured",
                    )
                    self.store.mark_not_dispatched(row.id, "ChannelNotConfigured")
                    continue
                delivery = (row.channel, row.turn_id)
                if delivery == previous_delivery:
                    delay = random.uniform(
                        *_message_gap_bounds(row.text, self.daemon_policy)
                    )
                    log_event(
                        logger,
                        TRACE,
                        "outbox_delay",
                        stage="delivery",
                        turn_id=row.turn_id,
                        channel=channel.name,
                        outbox_id=row.id,
                        delay_ms=int(delay * 1000),
                    )
                    await asyncio.sleep(delay)
                if not self.store.mark_sending(row.id):
                    continue
                attempt = row.attempts + 1
                send_started = monotonic()
                try:
                    log_event(
                        logger,
                        TRACE,
                        "outbox_send",
                        stage="delivery",
                        turn_id=row.turn_id,
                        channel=channel.name,
                        outbox_id=row.id,
                        attempt=attempt,
                        kind=row.kind,
                        content=safe_preview(row.text, 500),
                    )
                    await channel.send_message(
                        row.payload
                        or {
                            "action": "message",
                            "segments": [{"type": "text", "data": {"text": row.text}}],
                        }
                    )
                except NotConnected as error:
                    self.store.mark_not_dispatched(row.id, type(error).__name__)
                    log_event(
                        logger,
                        logging.WARNING,
                        "outbox_retry",
                        stage="delivery",
                        turn_id=row.turn_id,
                        channel=channel.name,
                        outbox_id=row.id,
                        attempt=attempt,
                        error_type=type(error).__name__,
                        duration_ms=int((monotonic() - send_started) * 1000),
                    )
                    continue
                except AmbiguousSend as error:
                    self.store.mark_ambiguous(row.id, attempt, type(error).__name__)
                    log_event(
                        logger,
                        logging.WARNING,
                        "outbox_ambiguous",
                        stage="delivery",
                        turn_id=row.turn_id,
                        channel=channel.name,
                        outbox_id=row.id,
                        attempt=attempt,
                        error_type=type(error).__name__,
                        duration_ms=int((monotonic() - send_started) * 1000),
                    )
                except SendRejected as error:
                    self.store.mark_failed(row.id, str(error))
                    log_event(
                        logger,
                        logging.WARNING,
                        "outbox_failure",
                        stage="delivery",
                        turn_id=row.turn_id,
                        channel=channel.name,
                        outbox_id=row.id,
                        attempt=attempt,
                        error_type=type(error).__name__,
                        reason=safe_preview(str(error), 300),
                        duration_ms=int((monotonic() - send_started) * 1000),
                    )
                else:
                    reply_waiting = self.store.mark_sent(row.id)
                    if reply_waiting:
                        self.agenda_changed.set()
                    previous_delivery = delivery
                    log_event(
                        logger,
                        logging.DEBUG,
                        "outbox_sent",
                        stage="delivery",
                        turn_id=row.turn_id,
                        channel=channel.name,
                        outbox_id=row.id,
                        attempt=attempt,
                        duration_ms=int((monotonic() - send_started) * 1000),
                    )
