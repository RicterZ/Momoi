import asyncio
import json
import logging
import random
from datetime import datetime
from typing import Any

from ..agenda_tools import AgendaTools
from ..builtin_tools import BuiltinTools
from ..channel import (
    AmbiguousSend,
    Channel,
    ChannelMessage,
    NotConnected,
    SendRejected,
    create_channel,
)
from ..config import AppConfig
from ..memory_tools import MemoryTools
from ..mcp_client import MCPManager
from ..models import IncomingMessage
from ..provider import AnthropicProvider, OpenAIProvider
from ..storage import Store
from ..webhooks import WebhookService
from .turns import TurnRunner

logger = logging.getLogger(__name__)
HEARTBEAT_QUEUE_ITEM = "__momoi_heartbeat__"
REFLECTION_QUEUE_PREFIX = "__momoi_reflection__:"
AGENDA_POLL_SECONDS = 5


class MomoiDaemon(TurnRunner):
    def __init__(self, config: AppConfig, channel: Channel | None = None) -> None:
        self.config = config
        self.store = Store(config.database, config.workspace)
        self.store.ensure_heartbeat(config.heartbeat)
        self.agenda_tools = AgendaTools(self.store)
        self.memory_tools = MemoryTools(self.store)
        self.builtin_tools = BuiltinTools()
        self.channel = channel or create_channel(config.channel)
        self.provider = (
            OpenAIProvider(config.llm)
            if config.llm.api_format == "openai"
            else AnthropicProvider(config.llm)
        )
        self.mcp = MCPManager(config.mcp_config)
        self.incoming: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        self.webhook_requests: asyncio.Queue[
            tuple[str, asyncio.Future[list[ChannelMessage]]]
        ] = asyncio.Queue()
        self.autonomous: asyncio.Queue[str] = asyncio.Queue()
        self.outbox_changed = asyncio.Event()
        self.agenda_changed = asyncio.Event()
        self._active_turn: asyncio.Task[None] | None = None
        self._stop_requested = False
        self.webhooks = (
            WebhookService(
                config.webhooks,
                self.channel.workflow_variables(),
                self.store,
                self._request_webhook_message,
                self.outbox_changed.set,
            )
            if config.webhooks.enabled
            else None
        )

    async def run(self, stop: asyncio.Event) -> None:
        logger.info("Channel started name=%s", self.channel.name)
        for event in self.store.pending_events():
            self.incoming.put_nowait(event)
        async with self.mcp, self.provider:
            tasks: list[asyncio.Task[None]] = []
            try:
                async with asyncio.TaskGroup() as group:
                    tasks.append(
                        group.create_task(self.channel.run(self._receive, stop))
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

    async def _receive(self, message: IncomingMessage) -> None:
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
        if self.store.add_event(message):
            logger.info("Accepted owner message channel=%s", message.channel)
            await self.incoming.put(message)

    async def _agent_worker(self, stop: asyncio.Event) -> None:
        batch: list[IncomingMessage] = []
        quiet_deadline = 0.0
        hard_deadline = 0.0
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            if not batch:
                kind, item = await self._next_work()
                if kind == "webhook":
                    prompt, future = item
                    if future.cancelled():
                        continue
                    try:
                        messages = await self._complete_webhook_message(prompt)
                    except asyncio.CancelledError:
                        if not future.done():
                            future.cancel()
                        raise
                    except Exception as error:
                        if not future.done():
                            future.set_exception(error)
                    else:
                        if not future.done():
                            future.set_result(messages)
                    continue
                if kind == "goal":
                    goal_id = str(item)
                    self._stop_requested = False
                    if goal_id == HEARTBEAT_QUEUE_ITEM:
                        work = self._complete_heartbeat_turn(stop)
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
                                self.config.heartbeat.min_interval_seconds
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
                now = loop.time()
                immediate = message.text.strip() == "/stop"
                quiet_deadline = now if immediate else now + self.channel.quiet_seconds
                hard_deadline = (
                    now if immediate else now + self.channel.max_batch_seconds
                )
                continue
            timeout = max(0.0, min(quiet_deadline, hard_deadline) - loop.time())
            try:
                message = await asyncio.wait_for(self.incoming.get(), timeout=timeout)
                if message.text.strip() == "/stop":
                    self.store.discard_events(batch)
                    batch = [message]
                    quiet_deadline = loop.time()
                    hard_deadline = quiet_deadline
                    continue
                batch.append(message)
                quiet_deadline = min(
                    loop.time() + self.channel.quiet_seconds, hard_deadline
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
        if not self.incoming.empty():
            return "owner", await self.incoming.get()
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

    async def _request_webhook_message(self, prompt: str) -> list[ChannelMessage]:
        future: asyncio.Future[list[ChannelMessage]] = (
            asyncio.get_running_loop().create_future()
        )
        await self.webhook_requests.put((prompt, future))
        return await future

    async def _scheduler_worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.agenda_changed.clear()
            reminder = self.store.claim_due_reminder()
            if reminder is not None:
                if self.store.fire_reminder(
                    str(reminder["id"]), self.config.notifications
                ):
                    logger.info("Fired reminder id=%s", reminder["id"])
                    self.outbox_changed.set()
                continue
            notification = self.store.claim_due_notification(self.config.notifications)
            if notification is not None:
                if self.store.queue_notification(
                    str(notification["id"]), config=self.config.notifications
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
        previous_turn_id: str | None = None
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
                if row.turn_id == previous_turn_id:
                    delay = random.uniform(2, 4)
                    logger.debug(
                        "Waiting %.2fs before next message channel=%s",
                        delay,
                        self.channel.name,
                    )
                    await asyncio.sleep(delay)
                self.store.mark_sending(row.id)
                attempt = row.attempts + 1
                try:
                    logger.debug(
                        "Sending message channel=%s kind=%s content=%s",
                        self.channel.name,
                        row.kind,
                        json.dumps(row.text, ensure_ascii=False),
                    )
                    await self.channel.send_message(
                        row.payload
                        or {
                            "action": "message",
                            "segments": [{"type": "text", "data": {"text": row.text}}],
                        }
                    )
                except NotConnected as error:
                    self.store.mark_not_dispatched(row.id, type(error).__name__)
                    break
                except AmbiguousSend as error:
                    self.store.mark_ambiguous(row.id, attempt, type(error).__name__)
                except SendRejected as error:
                    self.store.mark_failed(row.id, str(error))
                    logger.warning(
                        "Channel send rejected channel=%s outbox=%d error=%s",
                        self.channel.name,
                        row.id,
                        str(error),
                    )
                else:
                    self.store.mark_sent(row.id)
                    previous_turn_id = row.turn_id
                    logger.info("Sent outbox id=%d", row.id)
