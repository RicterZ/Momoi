from __future__ import annotations

import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from ..context_time import context_timestamp
from ..llm.accounting import UsageAccounting
from ..search import (
    SearchBackend,
    StringSearchBackend,
)
from .context_plans import ContextPlanStore
from .delivery import DeliveryStore
from .episode_search import (
    EpisodeQueryService,
    EpisodeSearchBackend,
    StringEpisodeSearchBackend,
)
from .episode_annealing import EpisodeAnnealingStore
from .episode_consolidation import EpisodeConsolidationStore
from .memory_operations import MemoryOperationStore
from .memory_mutations import MemoryMutationStore
from .memory_inventory import MemoryInventoryStore
from .memory_recall import MemoryRecallStore
from .memory_maintenance_commits import MemoryMaintenanceCommitStore
from .memory_maintenance_evidence import MemoryMaintenanceEvidenceStore
from .memory_maintenance_queue import MemoryMaintenanceQueueStore
from .goals import GoalStore
from .emotions import EmotionStore
from .turns import TurnStore
from .inbox import InboxStore
from .heartbeat_commits import HeartbeatCommitStore
from .heartbeat_schedule import HeartbeatScheduleStore
from .heartbeat_state import HeartbeatStateStore
from .semantic_queue import SemanticQueueStore
from .semantic_spaces import SemanticSpaceStore
from .semantic_sources import SemanticSourceStore
from .thinking import ThinkingStore
from .observability import ObservabilityStore
from .reflection_records import ReflectionRecordStore
from .reflection_schedule import ReflectionScheduleStore
from .reflection_source import ReflectionSourceStore
from .dashboard import DashboardStore
from .episode_lifecycle import EpisodeLifecycleStore
from .episode_links import EpisodeLinkStore
from .episode_plans import EpisodePlanStore
from .episode_records import EpisodeRecordStore
from .conversation_views import ConversationViewStore
from .transcripts import TranscriptStore
from .episode_queries import EpisodeQueryStore
from .episode_index import EpisodeIndexStore
from .notifications import NotificationStore
from .outbox import OutboxStore
from .reconciliation import ReconciliationStore
from .runtime_archives import RuntimeArchiveStore
from .turn_commits import TurnCommitStore
from .webhooks import WebhookStore
from .lifecycle import LifecycleStore

class Store(
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
        self._recover_emotion_outbox()
        self._recover_outbox()
        self._recover_webhooks()

    def set_usage_accounting(self, plugin: UsageAccounting) -> None:
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
