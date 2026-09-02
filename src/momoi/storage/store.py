from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from ..context_time import context_timestamp
from ..extensions.base import UsagePlugin
from ..policies import MemoryPolicy
from ..search import (
    SearchBackend,
    StringSearchBackend,
)
from .delivery import DeliveryStore
from .context_plans import ContextPlanStore
from .episode_search import (
    EpisodeQueryService,
    EpisodeSearchBackend,
    StringEpisodeSearchBackend,
)
from .episode_maintenance import EpisodeMaintenanceStore
from .memory import (
    MemoryStore,
)
from .memory_maintenance import MemoryMaintenanceStore
from .goals import GoalStore
from .emotions import EmotionStore
from .turns import EPISODE_MAINTENANCE_RESTART_REASON, TurnStore
from .inbox import InboxStore
from .heartbeat import HeartbeatStore
from .migrations import apply_migrations
from .semantic import SemanticStore
from .thinking import ThinkingStore
from .observability import ObservabilityStore
from .reflections import ReflectionStore
from .turn_workflow import turn_workflow_kind_sql
from .dashboard import DashboardStore
from .conversations import ConversationStore
from .conversation_views import ConversationViewStore
from .transcripts import TranscriptStore
from .episode_queries import EpisodeQueryStore
from .episode_index import EpisodeIndexStore
from .notifications import NotificationStore
from .outbox import OutboxStore
from .reconciliation import ReconciliationStore
from .turn_commits import TurnCommitStore




BASELINE_MOOD_STATE = "calm"
BASELINE_MOOD_INTENSITY = 0.35
BASELINE_MOOD_CAUSE = "resting baseline"
DEFAULT_ACTIVITY = "spending time freely"
class Store(
    GoalStore,
    EmotionStore,
    TurnStore,
    ObservabilityStore,
    ContextPlanStore,
    InboxStore,
    ReflectionStore,
    HeartbeatStore,
    DashboardStore,
    EpisodeMaintenanceStore,
    ConversationStore,
    ConversationViewStore,
    TranscriptStore,
    EpisodeQueryStore,
    EpisodeIndexStore,
    NotificationStore,
    OutboxStore,
    ReconciliationStore,
    TurnCommitStore,
    MemoryMaintenanceStore,
    MemoryStore,
    DeliveryStore,
    SemanticStore,
):
    def __init__(
        self,
        path: Path,
        workspace: Path | None = None,
        memory_policy: MemoryPolicy = MemoryPolicy(),
        search_backend: SearchBackend | None = None,
        episode_search_backend: EpisodeSearchBackend | None = None,
        thinking: Path | None = None,
        timezone: str = "UTC",
    ) -> None:
        database = Path(path).expanduser().resolve()
        self._workspace = (workspace or database.parent).expanduser().resolve()
        self._memory_policy = memory_policy
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
        self._usage_plugin: UsagePlugin | None = None
        self._initialize_database()
        self._recover_emotion_outbox()
        self._recover_outbox()
        self._recover_webhooks()

    def set_usage_plugin(self, plugin: UsagePlugin) -> None:
        self._usage_plugin = plugin

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

    def _initialize_database(self) -> None:
        self._db.executescript(Path(__file__).with_name("schema.sql").read_text())
        apply_migrations(self._db)
        now = time.time()
        self._normalize_goal_schedules(now)
        self._db.execute(
            """INSERT OR IGNORE INTO self_state
               (id, mood_state, mood_intensity, mood_cause, mood_updated_at,
                activity, activity_since, updated_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?)""",
            (
                BASELINE_MOOD_STATE,
                BASELINE_MOOD_INTENSITY,
                BASELINE_MOOD_CAUSE,
                now,
                DEFAULT_ACTIVITY,
                now,
                now,
            ),
        )
        self._db.execute("UPDATE goals SET review_claimed_at=NULL")
        self._db.execute(
            "UPDATE notifications SET claimed_at=NULL WHERE state='pending'"
        )
        self._supersede_heartbeat_contacts(
            ("heartbeat.chat", "heartbeat.reply_followup"),
            "process_restart_invalidated_ephemeral_contact",
            now,
        )
        self._db.execute(
            """UPDATE self_state SET heartbeat_claimed_at=NULL,
               heartbeat_claim_kind=NULL WHERE id=1"""
        )
        self._db.execute(
            "UPDATE reflections SET state='pending', claimed_at=NULL "
            "WHERE state='running'"
        )
        self._db.execute(
            "UPDATE conversation_episodes SET summary_claimed_at=NULL"
        )
        episode_workflow = turn_workflow_kind_sql("turns")
        self._db.execute(
            f"""UPDATE turns SET state='cancelled', stage='cancelled',
               failure_reason=?, updated_at=?
               WHERE kind='autonomous' AND state='running'
                 AND external_effect_started=0
                 AND {episode_workflow} IN (
                   'episode_consolidate', 'episode_anneal'
                 )""",
            (EPISODE_MAINTENANCE_RESTART_REASON, now),
        )
        self.recover_semantic_encoding()
        self._db.commit()

    def _recover_outbox(self) -> None:
        recovered = self._db.execute(
            "SELECT id FROM outbox WHERE state='sending'"
        ).fetchall()
        self._db.execute(
            """UPDATE outbox
               SET state='ambiguous', possible_duplicate=1, next_attempt_at=0
               WHERE state='sending' AND attempts < 2"""
        )
        self._db.execute(
            """UPDATE outbox SET state='failed', possible_duplicate=1
               WHERE state='sending' AND attempts >= 2"""
        )
        for row in recovered:
            outbox_id = int(row["id"])
            state = self._db.execute(
                "SELECT state FROM outbox WHERE id=?", (outbox_id,)
            ).fetchone()["state"]
            self._sync_outbox_message(outbox_id, str(state))
        self._db.commit()

    def _recover_webhooks(self) -> None:
        now = time.time()
        with self._db:
            running_execs = self._db.execute(
                "SELECT run_id, step_index FROM webhook_steps WHERE kind='exec' AND state='running'"
            ).fetchall()
            for row in running_execs:
                self._db.execute(
                    """UPDATE webhook_steps SET state='ambiguous',
                       error='process_interrupted', completed_at=?
                       WHERE run_id=? AND step_index=?""",
                    (now, row["run_id"], row["step_index"]),
                )
                self._db.execute(
                    """UPDATE webhook_runs SET state='ambiguous',
                       error='process_interrupted', updated_at=? WHERE id=?""",
                    (now, row["run_id"]),
                )
            self._db.execute(
                "UPDATE webhook_steps SET state='queued', started_at=NULL WHERE kind='message' AND state='running'"
            )
            self._db.execute(
                """UPDATE webhook_runs SET state='queued', updated_at=?
                   WHERE state='running' AND id NOT IN (
                       SELECT run_id FROM webhook_steps WHERE state='ambiguous'
                   )""",
                (now,),
            )
