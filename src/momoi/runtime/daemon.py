import asyncio
import json
import logging
import random
from collections import deque
from datetime import datetime
from typing import Any

from ..agenda_tools import AgendaTools
from ..builtin_tools import BuiltinTools
from ..channel import (
    AmbiguousSend,
    Channel,
    NotConnected,
    SendRejected,
    create_channel,
)
from ..config import AppConfig
from ..memory_tools import MemoryTools
from ..mcp_client import MCPManager
from ..models import AgentReply, IncomingMessage, OwnerInputStatus
from ..provider import AnthropicProvider, OpenAIProvider
from ..storage import Store
from ..webhooks import WebhookService
from .turns import TurnRunner

logger = logging.getLogger(__name__)
HEARTBEAT_QUEUE_ITEM = "__momoi_heartbeat__"
REFLECTION_QUEUE_PREFIX = "__momoi_reflection__:"
AGENDA_POLL_SECONDS = 5
MESSAGE_GAP_MIN_SECONDS = 4.0
MESSAGE_GAP_MAX_SECONDS = 7.0
MESSAGE_GAP_MIN_CHARS = 4
MESSAGE_GAP_SATURATION_CHARS = 60


def _message_gap_bounds(text: str) -> tuple[float, float]:
    ratio = min(
        1.0,
        max(
            0.0,
            (len(text.strip()) - MESSAGE_GAP_MIN_CHARS)
            / (MESSAGE_GAP_SATURATION_CHARS - MESSAGE_GAP_MIN_CHARS),
        ),
    )
    lower = MESSAGE_GAP_MIN_SECONDS + 2 * ratio
    upper = lower + 1
    return lower, min(MESSAGE_GAP_MAX_SECONDS, upper)


class MomoiDaemon(TurnRunner):
    def __init__(self, config: AppConfig, channel: Channel | None = None) -> None:
        self.config = config
        self._artifact_root().mkdir(parents=True, exist_ok=True)
        self.store = Store(config.database, config.workspace)
        self.store.ensure_heartbeat(config.heartbeat)
        self.agenda_tools = AgendaTools(self.store)
        self.memory_tools = MemoryTools(self.store)
        self.builtin_tools = BuiltinTools()
        created = (
            (channel,)
            if channel is not None
            else tuple(create_channel(item) for item in config.channel_configs)
        )
        self.channels = {item.name: item for item in created}
        if len(self.channels) != len(created):
            raise ValueError("channel plugin names must be unique")
        primary_name = str(getattr(config.channel, "plugin", ""))
        self.channel = self.channels.get(primary_name) or created[0]
        self.provider = (
            OpenAIProvider(config.llm)
            if config.llm.api_format == "openai"
            else AnthropicProvider(config.llm)
        )
        self.mcp = MCPManager(config.mcp_config)
        self.incoming: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        self._deferred_incoming: deque[IncomingMessage] = deque()
        self._owner_quiet_until: dict[str, float] = {}
        self._owner_activity_changed = asyncio.Event()
        self.webhook_requests: asyncio.Queue[
            tuple[str, str, asyncio.Future[AgentReply]]
        ] = asyncio.Queue()
        self.autonomous: asyncio.Queue[str] = asyncio.Queue()
        self.outbox_changed = asyncio.Event()
        self.agenda_changed = asyncio.Event()
        self._active_turn: asyncio.Task[None] | None = None
        self._stop_requested = False
        self._manual_heartbeat_channel: str | None = None
        self.webhooks = (
            WebhookService(
                config.webhooks,
                self._workflow_variables(),
                self.store,
                self._request_webhook_turn,
                self.outbox_changed.set,
                self.channel.name,
                config.heartbeat.reply_initial_interval_seconds,
            )
            if config.webhooks.enabled
            else None
        )

    async def run(self, stop: asyncio.Event) -> None:
        self.store.assign_legacy_outbox_channel(self.channel.name)
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
                    if self.webhooks is not None:
                        tasks.append(group.create_task(self.webhooks.run_api(stop)))
                        tasks.append(group.create_task(self.webhooks.run_worker(stop)))
                    await stop.wait()
                    for task in tasks:
                        task.cancel()
            finally:
                self.store.close()

    async def _run_channel(self, channel: Channel, stop: asyncio.Event) -> None:
        logger.info("Channel started name=%s", channel.name)
        try:
            await channel.run(self._receive, stop)
            if not stop.is_set():
                logger.error("Channel stopped unexpectedly name=%s", channel.name)
                await stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "Channel stopped name=%s error=%s",
                channel.name,
                type(error).__name__,
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

    def _touch_owner_activity(self, channel_name: str) -> None:
        channel = self._channel_for(channel_name)
        self._owner_quiet_until[channel.name] = (
            asyncio.get_running_loop().time() + channel.quiet_seconds
        )
        self._owner_activity_changed.set()

    async def _receive(self, event: IncomingMessage | OwnerInputStatus) -> None:
        if isinstance(event, OwnerInputStatus):
            self._touch_owner_activity(event.channel)
            logger.debug("Received owner input status channel=%s", event.channel)
            return
        message = event
        logger.debug(
            "Received owner message channel=%s message=%s",
            message.channel,
            json.dumps(message.text, ensure_ascii=False),
        )
        if message.text.strip() == "/stop":
            active = self._active_turn
            if active is not None and not active.done():
                self._stop_requested = True
                active.cancel()
            if self.store.add_event(message):
                logger.info("Accepted /stop owner command")
                await self.incoming.put(message)
            return
        if message.text.strip() == "/heartbeat":
            if self.store.claim_manual_heartbeat():
                logger.info("Accepted manual heartbeat command")
                self._manual_heartbeat_channel = message.channel
                await self.autonomous.put(HEARTBEAT_QUEUE_ITEM)
            else:
                logger.info("Ignored manual heartbeat command: heartbeat already active")
            return
        if self.store.add_event(message):
            logger.info("Accepted owner message channel=%s", message.channel)
            await self.incoming.put(message)
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
                    continue
                if kind == "goal":
                    goal_id = str(item)
                    self._stop_requested = False
                    if goal_id == HEARTBEAT_QUEUE_ITEM:
                        target_channel = self._manual_heartbeat_channel
                        self._manual_heartbeat_channel = None
                        work = self._complete_heartbeat_turn(stop, target_channel)
                    elif goal_id.startswith(REFLECTION_QUEUE_PREFIX):
                        work = self._complete_reflection_turn(
                            goal_id.removeprefix(REFLECTION_QUEUE_PREFIX), stop
                        )
                    else:
                        work = self._complete_goal_turn(goal_id, stop)
                    self._active_turn = asyncio.create_task(work)
                    try:
                        await self._active_turn
                    except asyncio.CancelledError:
                        if not self._stop_requested:
                            raise
                        if goal_id == HEARTBEAT_QUEUE_ITEM:
                            self.store.release_heartbeat_claim(
                                self._heartbeat_retry_delay()
                            )
                            logger.info("Active heartbeat turn stopped")
                        elif goal_id.startswith(REFLECTION_QUEUE_PREFIX):
                            local_date = goal_id.removeprefix(REFLECTION_QUEUE_PREFIX)
                            self.store.release_reflection(
                                local_date, "owner_stop", delay_seconds=3600
                            )
                            logger.info(
                                "Active daily reflection stopped date=%s", local_date
                            )
                        else:
                            self.store.release_goal_claim(goal_id, defer_seconds=900)
                            logger.info(
                                "Active autonomous turn stopped goal=%s",
                                goal_id,
                            )
                    finally:
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
                self._active_turn = asyncio.create_task(
                    self._complete_batch_turn(
                        sealed,
                        stop,
                        self._turn_id(*(event.event_id for event in sealed)),
                        self._channel_for(sealed[0].channel),
                    )
                )
                try:
                    await self._active_turn
                except asyncio.CancelledError:
                    if not self._stop_requested:
                        raise
                    self.store.cancel_turn(
                        self._turn_id(*(event.event_id for event in sealed)), sealed
                    )
                    logger.info("Active owner turn stopped")
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

    def _next_autonomous(self) -> str:
        return self._prioritize_autonomous(self.autonomous.get_nowait())

    def _prioritize_autonomous(self, item: str) -> str:
        if item != HEARTBEAT_QUEUE_ITEM or self.autonomous.empty():
            if not item.startswith(REFLECTION_QUEUE_PREFIX) or self.autonomous.empty():
                return item
            next_item = self.autonomous.get_nowait()
            if next_item == HEARTBEAT_QUEUE_ITEM:
                self.autonomous.put_nowait(next_item)
                return item
        else:
            next_item = self.autonomous.get_nowait()
        self.autonomous.put_nowait(item)
        return next_item

    async def _request_webhook_turn(
        self, prompt: str, turn_id: str
    ) -> AgentReply:
        future: asyncio.Future[AgentReply] = asyncio.get_running_loop().create_future()
        await self.webhook_requests.put((prompt, turn_id, future))
        return await future

    async def _scheduler_worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.agenda_changed.clear()
            reminder = self.store.claim_due_reminder()
            if reminder is not None:
                if self.store.fire_reminder(
                    str(reminder["id"]), self.config.notifications, self.channel.name
                ):
                    logger.info("Fired reminder id=%s", reminder["id"])
                    self.outbox_changed.set()
                continue
            notification = self.store.claim_due_notification(self.config.notifications)
            if notification is not None:
                if self.store.queue_notification(
                    str(notification["id"]),
                    config=self.config.notifications,
                    primary_channel=self.channel.name,
                ):
                    logger.info("Queued owner notification id=%s", notification["id"])
                    self.outbox_changed.set()
                continue
            goal = self.store.claim_due_goal()
            if goal is not None:
                await self.autonomous.put(str(goal["id"]))
                continue
            reflection = self.store.claim_due_reflection(
                self.config.reflection, self.config.notifications.timezone
            )
            if reflection is not None:
                await self.autonomous.put(
                    REFLECTION_QUEUE_PREFIX + str(reflection["local_date"])
                )
                continue
            heartbeat = self.store.claim_due_heartbeat(
                self.config.heartbeat, self.config.notifications
            )
            if heartbeat is not None:
                await self.autonomous.put(HEARTBEAT_QUEUE_ITEM)
                continue
            due_times = [
                due
                for due in (
                    self.store.next_reminder_due_at(),
                    self.store.next_notification_due_at(),
                    self.store.next_goal_due_at(),
                    self.store.next_reflection_due_at(
                        self.config.reflection,
                        self.config.notifications.timezone,
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
                max(0.0, due_at - datetime.now().timestamp()),
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
                    logger.error(
                        "Outbox target channel is not configured channel=%s outbox=%d",
                        row.channel,
                        row.id,
                    )
                    self.store.mark_not_dispatched(row.id, "ChannelNotConfigured")
                    continue
                delivery = (row.channel, row.turn_id)
                if delivery == previous_delivery:
                    delay = random.uniform(*_message_gap_bounds(row.text))
                    logger.debug(
                        "Waiting %.2fs before next message channel=%s",
                        delay,
                        channel.name,
                    )
                    await asyncio.sleep(delay)
                if not self.store.mark_sending(row.id):
                    continue
                attempt = row.attempts + 1
                try:
                    logger.debug(
                        "Sending message channel=%s kind=%s content=%s",
                        channel.name,
                        row.kind,
                        json.dumps(row.text, ensure_ascii=False),
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
                    continue
                except AmbiguousSend as error:
                    self.store.mark_ambiguous(row.id, attempt, type(error).__name__)
                except SendRejected as error:
                    self.store.mark_failed(row.id, str(error))
                    logger.warning(
                        "Channel send rejected channel=%s outbox=%d error=%s",
                        channel.name,
                        row.id,
                        str(error),
                    )
                else:
                    reply_waiting = self.store.mark_sent(
                        row.id,
                        self.config.heartbeat.reply_initial_interval_seconds,
                    )
                    if reply_waiting:
                        self.agenda_changed.set()
                    previous_delivery = delivery
                    logger.info("Sent outbox id=%d", row.id)
