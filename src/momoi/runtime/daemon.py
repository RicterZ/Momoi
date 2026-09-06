import asyncio
import logging
from collections import deque
from typing import Any

from ..tools.agenda import AgendaTools
from ..integrations.contracts.asr import ASRProvider
from ..integrations.contracts.tts import TTSProvider
from ..integrations.registry import ServiceRegistry
from ..tools.builtin import BuiltinTools
from ..channel import (
    Channel,
    ChannelDependencies,
    create_channel,
)
from ..config.models import AppConfig
from ..dashboard.service import DashboardService
from ..dashboard.settings import DashboardSettings
from ..observability.events import log_event
from ..tools.memory import MemoryTools
from ..tools.thinking import ThinkingTools
from ..mcp.manager import MCPManager
from ..models import AgentReply, IncomingMessage
from ..semantic.service import SemanticRecallService
from ..storage import Store
from ..webhooks.service import WebhookService
from .jobs import AutonomousJob
from .agent.result_store import ToolResultStore
from .agent.context_window import ContextWindow
from .agent.delivery import BubbleDelivery, DeliveryPolicy
from .agent.model_round import ModelRoundRunner
from .agent.tool_batch import ToolBatchExecutor
from .agent.tool_executor import ToolExecutor, artifact_root, tool_result_root
from .agent.tool_surface import ToolSurface
from .dispatch import AgentWorker, CommandRouter, OutboxWorker, Scheduler
from .turns import TurnRunner
from .workflows.owner import OwnerUpdateController

logger = logging.getLogger(__name__)


class MomoiDaemon(
    CommandRouter,
    AgentWorker,
    Scheduler,
    OutboxWorker,
    TurnRunner,
):
    def __init__(
        self,
        config: AppConfig,
        channel: Channel | None = None,
        dashboard: tuple[str, int] | None = None,
        asr_provider: ASRProvider | None = None,
        tts_provider: TTSProvider | None = None,
    ) -> None:
        self.config = config
        self._loaded_workspace_prompts: dict[str, str] = {}
        self.daemon_policy = config.policies.daemon
        artifact_root(config).mkdir(parents=True, exist_ok=True)
        self.tool_results = ToolResultStore(
            tool_result_root(config),
            retention_days=config.tool_result_retention_days,
        )
        self.store = Store(
            config.database,
            config.workspace,
            config.policies.memory,
            thinking=config.thinking,
            timezone=config.timezone,
        )
        self.services = ServiceRegistry(
            config.providers,
            dump_dir=config.workspace / "llm-dumps" if config.workspace else None,
            semantic_policy=config.policies.semantic,
            overrides={
                k: v
                for k, v in {"asr": asr_provider, "tts": tts_provider}.items()
                if v is not None
            },
        )
        self.semantic_recall = SemanticRecallService(
            self.store,
            self.services.embedding_config,
            policy=config.policies.semantic,
            client=self.services.embedding,
        )
        self.semantic_recall.start()
        accounting = self.services.llm.accounting
        if accounting is not None:
            self.store.set_usage_accounting(accounting)
        self.store.ensure_heartbeat(config.heartbeat)
        self.agenda_tools = AgendaTools(self.store)
        self.memory_tools = MemoryTools(
            self.store, config.policies.memory, self.semantic_recall
        )
        self.thinking_tools = ThinkingTools(self.store)
        self.builtin_tools = BuiltinTools(
            config.workspace or config.database.parent,
            private_roots=(tool_result_root(config),),
        )
        self.asr_provider = self.services.asr

        def build_channel(item: object) -> Channel:
            dependencies = (
                ChannelDependencies(
                    asr_provider=self.asr_provider,
                    asr_max_audio_bytes=config.providers.options_for("asr").get(
                        "max_audio_bytes", 3 * 1024 * 1024
                    ),
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
        self.provider = self.services.llm
        self.provider.usage_sink = self.store.record_llm_call
        self.provider.thinking_sink = self.store.record_thinking_call
        if accounting is not None:
            self.provider.usage_parser = accounting.parse_usage
        self.dashboard = (
            DashboardService(
                self.store,
                *dashboard,
                token=config.dashboard.token,
                balance_provider=self.services.balance,
                settings=DashboardSettings.from_config(config),
            )
            if dashboard is not None
            else None
        )
        self.mcp = MCPManager(config.mcp_config)
        self.tool_surface = ToolSurface(
            self.mcp,
            self.channels,
            voice_enabled=self.services.tts is not None,
        )
        self.delivery_policy = DeliveryPolicy(config, self.store)
        self.tool_executor = ToolExecutor(
            config,
            self.store,
            self.mcp,
            self.builtin_tools,
            self.agenda_tools,
            self.tool_results,
        )
        self.context_window = ContextWindow(config, self.store, self.tool_results)
        self.model_round = ModelRoundRunner(self.context_window, self.store)
        self.incoming: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        self._deferred_incoming: deque[IncomingMessage] = deque()
        self._owner_quiet_until: dict[str, float] = {}
        self._last_owner_activity_at = 0.0
        self._owner_activity_changed = asyncio.Event()
        self._owner_message_changed = asyncio.Event()
        self.owner_updates = OwnerUpdateController(
            self.incoming,
            self._deferred_incoming,
            self._owner_quiet_until,
            self._owner_activity_changed,
            self._owner_message_changed,
            self._channel_for,
        )
        self.webhook_requests: asyncio.Queue[
            tuple[str, str, asyncio.Future[AgentReply]]
        ] = asyncio.Queue()
        self.autonomous: asyncio.Queue[AutonomousJob] = asyncio.Queue()
        self.episode_annealing_requested = asyncio.Event()
        self.outbox_changed = asyncio.Event()
        self.bubble_delivery = BubbleDelivery(
            self.store,
            self.channels,
            self.delivery_policy,
            self.outbox_changed,
            tts_provider=self.services.tts,
        )
        self.tool_batch = ToolBatchExecutor(
            config,
            self.store,
            self.tool_surface,
            self.tool_executor,
            self.bubble_delivery,
            self.agenda_tools,
            self.memory_tools,
            self.thinking_tools,
            self.tool_results,
            self.outbox_changed,
        )
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
        try:
            async with self.mcp, self.services:
                tasks: list[asyncio.Task[None]] = []
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
