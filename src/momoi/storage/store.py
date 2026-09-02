from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING

from ..channel import (
    ChannelMessage,
    media_path,
    normalize_channel_message,
    render_channel_message,
)
from ..context_time import context_timestamp
from ..emotions import emotion_slug
from ..extensions.base import UsagePlugin
from ..models import (
    AgentReply,
    IncomingMessage,
    TurnDraft,
)
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
from .scheduling import next_schedule_at
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

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass




BASELINE_MOOD_STATE = "calm"
BASELINE_MOOD_INTENSITY = 0.35
BASELINE_MOOD_CAUSE = "resting baseline"
DEFAULT_ACTIVITY = "spending time freely"
def _owner_message_created_at(
    events: list[IncomingMessage], now: float
) -> float:
    times = [
        float(event.occurred_at or event.received_at)
        for event in events
        if event.occurred_at or event.received_at
    ]
    return min(times) if times else now












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

    @staticmethod
    def _message_delivery_state(
        outbox_state: str, possible_duplicate: bool = False
    ) -> str:
        if possible_duplicate and outbox_state != "sent":
            return "uncertain"
        return {
            "sent": "delivered",
            "ambiguous": "uncertain",
            "failed": "failed",
            "superseded": "failed",
        }.get(outbox_state, "queued")

    def _sync_outbox_message(self, outbox_id: int, outbox_state: str) -> None:
        episodes = self._db.execute(
            """SELECT DISTINCT et.episode_id FROM messages AS m
               JOIN episode_turns AS et ON et.turn_id=m.turn_id
               WHERE m.outbox_id=?""",
            (outbox_id,),
        ).fetchall()
        outbox = self._db.execute(
            "SELECT possible_duplicate FROM outbox WHERE id=?", (outbox_id,)
        ).fetchone()
        self._db.execute(
            "UPDATE messages SET delivery_state=? WHERE outbox_id=?",
            (
                self._message_delivery_state(
                    outbox_state,
                    bool(outbox and outbox["possible_duplicate"]),
                ),
                outbox_id,
            ),
        )
        for row in episodes:
            self._reindex_episode_terms(str(row["episode_id"]))

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


























































    def open_reconciliation(self, turn_id: str, reason: str) -> None:
        with self._db:
            self._open_reconciliation(turn_id, reason, time.time())

    def _open_reconciliation(self, turn_id: str, reason: str, now: float) -> None:
        self._db.execute(
            """INSERT INTO reconciliations
               (turn_id, status, reason, resolution, created_at, updated_at)
               VALUES (?, 'open', ?, '', ?, ?)
               ON CONFLICT(turn_id) DO UPDATE SET status='open', reason=excluded.reason,
                 resolution='', updated_at=excluded.updated_at""",
            (turn_id, reason[:500], now, now),
        )

    def resolve_reconciliation(
        self, turn_prefix: str, resolution: str, *, resume: bool
    ) -> dict[str, object]:
        prefix = turn_prefix.strip()
        resolution = resolution.strip()
        if len(prefix) < 8 or not re.fullmatch(r"[0-9a-f]+", prefix):
            raise ValueError(
                "turn id prefix must contain at least 8 hexadecimal characters"
            )
        if not resolution:
            raise ValueError("resolution is required")
        rows = self._db.execute(
            """SELECT * FROM reconciliations
               WHERE status='open' AND turn_id LIKE ? ORDER BY created_at""",
            (f"{prefix}%",),
        ).fetchall()
        if not rows:
            raise ValueError("open reconciliation not found")
        if len(rows) > 1:
            raise ValueError("turn id prefix is ambiguous")
        row = rows[0]
        status = "resumed" if resume else "resolved"
        with self._db:
            self._db.execute(
                """UPDATE reconciliations SET status=?, resolution=?, updated_at=?
                   WHERE turn_id=?""",
                (status, resolution[:2000], time.time(), row["turn_id"]),
            )
        return {
            **dict(row),
            "status": status,
            "resolution": resolution[:2000],
        }































    def _outbox_content(
        self, message: ChannelMessage
    ) -> tuple[str, str, str | None, dict[str, object]]:
        slug = emotion_slug(message) if isinstance(message, str) else None
        if slug is not None:
            asset = self.emotion(slug)
            if asset is None:
                raise ValueError(f"unknown emotion slug: {slug}")
            path = str(asset["path"])
            path = self._stored_asset_path(path)
            payload: dict[str, object] = {
                "action": "message",
                "segments": [{"type": "image", "data": {"file": path}}],
            }
            return message, "image", path, payload
        payload = normalize_channel_message(message)
        text = message if isinstance(message, str) else render_channel_message(payload)
        if payload["action"] == "forward":
            kind = "forward"
        else:
            segments = payload.get("segments") or []
            kind = str(segments[0].get("type")) if len(segments) == 1 else "message"
        return text, kind, media_path(payload), payload

    def queue_progress(
        self,
        turn_id: str,
        tool_call_id: str,
        messages: list[ChannelMessage],
        target_channel: str = "",
    ) -> None:
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE turns SET stage='message_dispatch', updated_at=?
                   WHERE id=? AND state='running'""",
                (now, turn_id),
            )
            for index, message in enumerate(messages):
                text, kind, path, payload = self._outbox_content(message)
                self._db.execute(
                    """INSERT OR IGNORE INTO turn_progress
                       (turn_id, tool_call_id, part_index, text, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (turn_id, tool_call_id, index, text, now),
                )
                self._db.execute(
                    """INSERT OR IGNORE INTO outbox
                       (turn_id, dedupe_key, text, kind, media_path, payload_json,
                        target_channel)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        turn_id,
                        f"turn:{turn_id}:progress:{tool_call_id}:{index}",
                        text,
                        kind,
                        path,
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        target_channel,
                    ),
                )

    def commit_turn(
        self,
        events: list[IncomingMessage],
        user_text: str,
        reply: AgentReply,
        draft: TurnDraft | None = None,
        turn_id: str | None = None,
        target_channel: str = "",
    ) -> str:
        assistant_messages = reply.messages
        normalized_messages = [
            self._outbox_content(message) for message in assistant_messages
        ]
        turn_id = turn_id or uuid.uuid4().hex
        event_ids = [event.event_id for event in events]
        now = time.time()
        user_created_at = _owner_message_created_at(events, now)
        with self._db:
            source_json = json.dumps(event_ids, ensure_ascii=False)
            self._db.execute(
                """INSERT OR IGNORE INTO turns
                   (id, kind, workflow_kind, source_ids_json, state,
                    started_at, updated_at)
                   VALUES (?, 'owner', 'owner', ?, 'running', ?, ?)""",
                (turn_id, source_json, now, now),
            )
            progress = self._db.execute(
                """SELECT text, created_at, tool_call_id, part_index
                   FROM turn_progress
                   WHERE turn_id=? ORDER BY created_at, tool_call_id, part_index""",
                (turn_id,),
            ).fetchall()
            raw_text = "\n".join(
                [
                    user_text,
                    *(str(row["text"]) for row in progress),
                    *(text for text, _kind, _path, _payload in normalized_messages),
                ]
            )
            self._apply_context_plan_episodes(
                turn_id,
                now,
                raw_text,
                keep_open=reply.should_schedule_reply_wait,
            )
            self._db.execute(
                """INSERT INTO messages
                   (turn_id, role, content, created_at, source_event_ids_json)
                   VALUES (?, 'user', ?, ?, ?)""",
                (
                    turn_id,
                    user_text,
                    user_created_at,
                    source_json,
                ),
            )
            for row in progress:
                outbox = self._db.execute(
                    """SELECT id, state, possible_duplicate FROM outbox
                       WHERE dedupe_key=?""",
                    (
                        f"turn:{turn_id}:progress:{row['tool_call_id']}:"
                        f"{row['part_index']}",
                    ),
                ).fetchone()
                self._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at, source_event_ids_json,
                        outbox_id, delivery_state)
                       VALUES (?, 'assistant', ?, ?, ?, ?, ?)""",
                    (
                        turn_id,
                        row["text"],
                        row["created_at"],
                        source_json,
                        outbox["id"] if outbox else None,
                        self._message_delivery_state(
                            str(outbox["state"]),
                            bool(outbox["possible_duplicate"]),
                        )
                        if outbox
                        else "uncertain",
                    ),
                )
            for index, (assistant_text, kind, path, payload) in enumerate(
                normalized_messages
            ):
                outbox = self._db.execute(
                    """INSERT INTO outbox
                       (turn_id, dedupe_key, text, kind, media_path, payload_json,
                        reply_expectation, target_channel)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        turn_id,
                        f"turn:{turn_id}:{index}",
                        assistant_text,
                        kind,
                        path,
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        "",
                        target_channel,
                    ),
                )
                self._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at, source_event_ids_json,
                        outbox_id, delivery_state)
                       VALUES (?, 'assistant', ?, ?, ?, ?, 'queued')""",
                    (
                        turn_id,
                        assistant_text,
                        now,
                        source_json,
                        outbox.lastrowid,
                    ),
                )
            self._index_turn_episode_terms(turn_id)
            self._apply_mood_update(reply.mood_update, now)
            if reply.activity_update is not None:
                current_activity = self._db.execute(
                    "SELECT activity, activity_since FROM self_state WHERE id=1"
                ).fetchone()
                activity_text = str(reply.activity_update["text"])
                activity_since = (
                    current_activity["activity_since"]
                    if current_activity is not None
                    and current_activity["activity"] == activity_text
                    else now
                )
                self._db.execute(
                    """UPDATE self_state SET activity=?, activity_result=?,
                       activity_since=?, updated_at=? WHERE id=1""",
                    (
                        activity_text,
                        str(reply.activity_update["result"])[:2000],
                        activity_since,
                        now,
                    ),
                )
            for memory in draft.memories if draft else []:
                self._remember(memory, events, now)
            for forgotten in draft.forgotten_memories if draft else []:
                self._forget_memory(forgotten, events, now)
            self._apply_goal_mutations(draft, now)
            self._apply_cooled_reply_action(draft, now)
            self._append_turn_journal(
                turn_id,
                "final",
                {
                    "channel": target_channel,
                    "reply_wait": reply.reply_wait,
                    "mood_change": reply.mood_update,
                    **(
                        {"activity_change": reply.activity_update}
                        if reply.activity_update
                        else {}
                    ),
                    "mutations": {
                        "memories": [
                            vars(memory)
                            for memory in (draft.memories if draft else [])
                        ],
                        "forgotten_memories": [
                            vars(memory)
                            for memory in (
                                draft.forgotten_memories if draft else []
                            )
                        ],
                        "goals": list(draft.goals.values()) if draft else [],
                    },
                },
                visibility="internal",
                trust="runtime",
                created_at=now,
            )
            self._db.executemany(
                "UPDATE events SET processed=1 WHERE id=?",
                ((event_id,) for event_id in event_ids),
            )
            if reply.should_schedule_reply_wait:
                self._bind_turn_reply_expectation(
                    turn_id,
                    reply.reply_expectation,
                    reply.reply_wait_delay_minutes,
                    reply.reply_wait_reason,
                )
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   source_ids_json=?, failure_reason=NULL, updated_at=? WHERE id=?""",
                (source_json, now, turn_id),
            )
        return turn_id

    def commit_autonomous_turn(
        self,
        goal_id: str,
        draft: TurnDraft,
        turn_id: str | None = None,
        notification_channel: str = "",
    ) -> str:
        turn_id = turn_id or uuid.uuid4().hex
        now = time.time()
        with self._db:
            self._apply_goal_mutations(draft, now)
            if goal_id not in draft.goals:
                current = self.goal(goal_id)
                next_review_at = (
                    next_schedule_at(current["schedule"], self._timezone, now)
                    if current and current.get("schedule")
                    else now + 900
                )
                self._db.execute(
                    """UPDATE goals SET review_claimed_at=NULL, next_review_at=?,
                       retry_at=NULL, failure_count=0, updated_at=?
                       WHERE id=? AND status IN ('active', 'waiting')""",
                    (next_review_at, now, goal_id),
                )
            current = self.goal(goal_id)
            if current is not None:
                goal_record = (
                    "[AUTONOMOUS GOAL REVIEW RECORD; not sent to the owner]\n"
                    f"Goal: {current['title']}\n"
                    f"Status: {current['status']}\n"
                    f"Latest result: {current['latest_result'] or '(none)'}\n"
                    f"Next action: {current['next_action'] or '(none)'}"
                )
                goal_source = json.dumps([f"goal-record:{turn_id}"])
                self._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at, source_event_ids_json,
                        delivery_state)
                       SELECT ?, 'assistant', ?, ?, ?, 'internal'
                       WHERE NOT EXISTS (
                           SELECT 1 FROM messages
                           WHERE turn_id=? AND source_event_ids_json=?
                       )""",
                    (
                        turn_id,
                        goal_record,
                        now,
                        goal_source,
                        turn_id,
                        goal_source,
                    ),
                )
                archive_day = self._archive_day(now)
                self._ensure_runtime_archive(
                    archive_kind="goal",
                    archive_day=archive_day,
                    episode_key=f"goal:{goal_id}:day:{archive_day}",
                    turn_id=turn_id,
                    title=str(current["title"]),
                    now=now,
                    recall_values=(goal_record,),
                )
            if draft.notification_messages:
                self._db.execute(
                    """INSERT OR IGNORE INTO notifications
                       (id, turn_id, goal_id, notification_key, priority, reason,
                        messages_json, state, not_before, created_at, target_channel)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                    (
                        f"notification:{turn_id}",
                        turn_id,
                        goal_id,
                        draft.notification_key or f"goal.{goal_id}",
                        draft.notification_priority,
                        draft.notification_reason,
                        json.dumps(draft.notification_messages, ensure_ascii=False),
                        now,
                        now,
                        notification_channel,
                    ),
                )
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   failure_reason=NULL, updated_at=? WHERE id=?""",
                (now, turn_id),
            )
        return turn_id
