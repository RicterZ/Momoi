from __future__ import annotations

import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from ..llm.accounting import UsageAccounting
from .core.search import (
    SearchBackend,
    StringSearchBackend,
)
from .context.context_plans import ContextPlanStore
from .memory.current_state import CurrentStateManager
from .memory.current_state_tasks import CurrentStateTaskStore
from .delivery.delivery import DeliveryStore
from .episode.episode_search import (
    EpisodeQueryService,
    EpisodeSearchBackend,
    StringEpisodeSearchBackend,
)
from .episode.episode_annealing import EpisodeAnnealingStore
from .episode.episode_consolidation import EpisodeConsolidationStore
from .memory.memory_operations import MemoryOperationStore
from .memory.memory_mutations import MemoryMutationStore
from .memory.memory_inventory import MemoryInventoryStore
from .memory.memory_recall import MemoryRecallStore
from .memory.memory_maintenance_commits import MemoryMaintenanceCommitStore
from .memory.memory_maintenance_evidence import MemoryMaintenanceEvidenceStore
from .memory.memory_maintenance_queue import MemoryMaintenanceQueueStore
from .agenda.crontabs import GoalStore
from .delivery.emotions import EmotionStore
from .conversation.turns import TurnStore
from .conversation.inbox import InboxStore
from .agenda.heartbeat_commits import HeartbeatCommitStore
from .agenda.heartbeat_schedule import HeartbeatScheduleStore
from .agenda.heartbeat_state import HeartbeatStateStore
from .semantic.semantic_queue import SemanticQueueStore
from .semantic.semantic_spaces import SemanticSpaceStore
from .semantic.semantic_sources import SemanticSourceStore
from .ops.thinking import ThinkingStore
from .ops.observability import ObservabilityStore
from .reflection.reflection_records import ReflectionRecordStore
from .reflection.reflection_schedule import ReflectionScheduleStore
from .reflection.reflection_source import ReflectionSourceStore
from .ops.dashboard import DashboardStore
from .episode.episode_lifecycle import EpisodeLifecycleStore
from .episode.episode_links import EpisodeLinkStore
from .episode.episode_plans import EpisodePlanStore
from .episode.episode_records import EpisodeRecordStore
from .conversation.conversation_views import ConversationViewStore
from .core.timestamps import context_timestamp
from .conversation.transcripts import TranscriptStore
from .episode.episode_queries import EpisodeQueryStore
from .episode.episode_index import EpisodeIndexStore
from .agenda.notifications import NotificationStore
from .delivery.outbox import OutboxStore
from .ops.reconciliation import ReconciliationStore
from .ops.runtime_archives import RuntimeArchiveStore
from .conversation.turn_commits import TurnCommitStore
from .ops.webhooks import WebhookStore
from .core.lifecycle import LifecycleStore

from .agenda.plans import PlanStore

class Store(
    PlanStore,
    CurrentStateTaskStore,
    LifecycleStore,
    GoalStore,
    EmotionStore,
    TurnStore,
    ObservabilityStore,
    ContextPlanStore,
    InboxStore,
    ReflectionScheduleStore,
    ReflectionSourceStore,
    ReflectionRecordStore,
    HeartbeatStateStore,
    HeartbeatScheduleStore,
    HeartbeatCommitStore,
    DashboardStore,
    EpisodeConsolidationStore,
    EpisodeAnnealingStore,
    EpisodeRecordStore,
    EpisodeLinkStore,
    EpisodeLifecycleStore,
    EpisodePlanStore,
    RuntimeArchiveStore,
    ConversationViewStore,
    TranscriptStore,
    EpisodeQueryStore,
    EpisodeIndexStore,
    NotificationStore,
    OutboxStore,
    ReconciliationStore,
    TurnCommitStore,
    MemoryMaintenanceQueueStore,
    MemoryMaintenanceEvidenceStore,
    MemoryMaintenanceCommitStore,
    MemoryInventoryStore,
    MemoryRecallStore,
    MemoryMutationStore,
    MemoryOperationStore,
    WebhookStore,
    DeliveryStore,
    SemanticSpaceStore,
    SemanticSourceStore,
    SemanticQueueStore,
):
    def __init__(
        self,
        path: Path,
        workspace: Path | None = None,
        search_backend: SearchBackend | None = None,
        episode_search_backend: EpisodeSearchBackend | None = None,
        thinking: Path | None = None,
        timezone: str = "UTC",
    ) -> None:
        database = Path(path).expanduser().resolve()
        self._workspace = (workspace or database.parent).expanduser().resolve()
        self._search_backend = search_backend or StringSearchBackend()
        self._timezone = ZoneInfo(timezone)
        self._episode_query = EpisodeQueryService(
            episode_search_backend
            or StringEpisodeSearchBackend(self._search_backend)
        )
        self._db = sqlite3.connect(database)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._thinking = ThinkingStore(
            Path(thinking) if thinking is not None else database.parent,
            self._timezone,
            self._search_backend,
        )
        self._usage_accounting: UsageAccounting | None = None
        self._initialize_database()
        self.current_state = CurrentStateManager(self._db)
        self._recover_emotion_outbox()
        self._recover_outbox()
        self._recover_webhooks()

    def set_usage_accounting(self, plugin: UsageAccounting | None) -> None:
        self._usage_accounting = plugin

    @property
    def search_backend(self) -> SearchBackend:
        return self._search_backend

    @property
    def timezone(self) -> ZoneInfo:
        return self._timezone

    def context_timestamp(self, value: object) -> str:
        return context_timestamp(value, self._timezone)

    def close(self) -> None:
        self._thinking.close()
        self._db.close()
