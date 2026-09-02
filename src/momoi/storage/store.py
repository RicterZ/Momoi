from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING

from ..channel import (
    ChannelMessage,
    media_path,
    normalize_channel_message,
    render_channel_message,
)
from ..config import NotificationConfig
from ..context_time import context_timestamp
from ..emotions import emotion_slug
from ..extensions.base import UsagePlugin
from ..logging_context import log_event
from ..models import (
    AgentReply,
    IncomingMessage,
    TurnDraft,
)
from ..policies import MemoryPolicy
from ..search import (
    SearchBackend,
    StringSearchBackend,
    search_alternatives,
)
from .delivery import DeliveryStore
from .context_plan_adapter import normalize_context_plan
from .context_plans import ContextPlanStore, recall_query_texts
from .episode_search import (
    EpisodeQueryService,
    EpisodeSearchBackend,
    EpisodeSearchDocument,
    EpisodeSearchField,
    EpisodeSearchMessage,
    StringEpisodeSearchBackend,
)
from .episode_ranking import EpisodeRecallQuery, rank_episode_matches
from .episode_sql import runtime_archive_kind_sql
from .episode_maintenance import EpisodeMaintenanceStore
from .memory import (
    MemoryStore,
    estimate_tokens,
    token_chunk,
    truncate_tokens,
)
from .memory_maintenance import MemoryMaintenanceStore
from .goals import GoalStore
from .emotions import EmotionStore
from .turns import EPISODE_MAINTENANCE_RESTART_REASON, TurnStore
from .integrity import decode_stored_json
from .inbox import InboxStore
from .heartbeat import HeartbeatStore
from .migrations import apply_migrations
from .semantic import SemanticStore
from .scheduling import next_schedule_at, quiet_until
from .thinking import ThinkingStore
from .observability import ObservabilityStore
from .reflections import ReflectionStore
from .turn_workflow import turn_workflow_kind_sql
from .timestamps import add_context_timestamps
from .dashboard import DashboardStore

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..semantic import DenseRecallEvidence




BASELINE_MOOD_STATE = "calm"
BASELINE_MOOD_INTENSITY = 0.35
BASELINE_MOOD_CAUSE = "resting baseline"
DEFAULT_ACTIVITY = "spending time freely"
# Delivery and turn-control calls are protocol, not work worth recalling.
TRANSCRIPT_PROTOCOL_TOOLS = frozenset(
    {
        "send_bubbles",
        "end_turn",
        "autonomous_finish",
        "heartbeat_end_turn",
        "tool_enable",
        "read_tool_result",
    }
)
# Argument names that usually identify what a call was actually about.
_TOOL_SUBJECT_KEYS = (
    "query",
    "q",
    "expression",
    "url",
    "path",
    "title",
    "name",
    "key",
    "keyword",
    "command",
)


def _tool_call_subject(arguments: object, limit: int = 48) -> str:
    """Pick the argument that identifies what a call was about."""

    if not isinstance(arguments, dict):
        return ""
    for key in _TOOL_SUBJECT_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return truncate_tokens(" ".join(value.split()), limit)
    for value in arguments.values():
        if isinstance(value, str) and value.strip():
            return truncate_tokens(" ".join(value.split()), limit)
    return ""


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

    def episodes_for_turns(
        self, turn_ids: list[str]
    ) -> dict[str, dict[str, str]]:
        ids = [turn_id for turn_id in dict.fromkeys(turn_ids) if turn_id]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self._db.execute(
            f"""SELECT et.turn_id, e.id, e.title
                FROM episode_turns AS et
                JOIN conversation_episodes AS e ON e.id=et.episode_id
                WHERE et.turn_id IN ({placeholders})
                ORDER BY et.relation='primary' DESC, et.ordinal""",
            tuple(ids),
        ).fetchall()
        found: dict[str, dict[str, str]] = {}
        for row in rows:
            turn_id = str(row["turn_id"])
            if turn_id not in found:
                found[turn_id] = {
                    "episode_id": str(row["id"]),
                    "episode_title": str(row["title"]),
                }
        return found

    def _episode_dict(self, row: sqlite3.Row) -> dict[str, object]:
        episode = dict(row)
        episode_id = episode.get("id")
        episode.pop("overlap", None)
        add_context_timestamps(
            episode,
            ("created_at", "updated_at", "closed_at", "summary_abandoned_at"),
            self._timezone,
        )
        episode["working_summary_claims"] = decode_stored_json(
            episode.pop("working_summary_claims_json"),
            entity="conversation_episode",
            record_id=episode_id,
            field="working_summary_claims_json",
            expected_type=list,
            fallback=[],
        )
        episode["emotional_context"] = decode_stored_json(
            episode.pop("emotional_context_json"),
            entity="conversation_episode",
            record_id=episode_id,
            field="emotional_context_json",
            expected_type=dict,
            fallback={},
        )
        episode["outcomes"] = decode_stored_json(
            episode.pop("outcomes_json"),
            entity="conversation_episode",
            record_id=episode_id,
            field="outcomes_json",
            expected_type=list,
            fallback=[],
        )
        episode.pop("summary", None)
        for name in ("topics", "entities", "open_loops"):
            episode[name] = json.loads(str(episode.pop(f"{name}_json")))
        return episode

    @staticmethod
    def _recall_terms(*values: object) -> set[str]:
        terms: set[str] = set()

        def collect(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    collect(key)
                    collect(item)
                return
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    collect(item)
                return
            terms.update(search_alternatives(str(value or "")))

        for value in values:
            collect(value)
        return terms

    def _episode_recall_key(self, episode_id: str) -> int:
        self._db.execute(
            """INSERT OR IGNORE INTO recall_episode_ids (episode_id)
               VALUES (?)""",
            (episode_id,),
        )
        row = self._db.execute(
            "SELECT id FROM recall_episode_ids WHERE episode_id=?",
            (episode_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("episode recall id was not created")
        return int(row["id"])

    def _recall_term_ids(self, terms: set[str], *, create: bool) -> dict[str, int]:
        if not terms:
            return {}
        ordered = sorted(terms)
        if create:
            self._db.executemany(
                "INSERT OR IGNORE INTO recall_terms (term) VALUES (?)",
                ((term,) for term in ordered),
            )
        resolved: dict[str, int] = {}
        for offset in range(0, len(ordered), 500):
            chunk = ordered[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._db.execute(
                f"SELECT id, term FROM recall_terms WHERE term IN ({placeholders})",
                chunk,
            ).fetchall()
            resolved.update({str(row["term"]): int(row["id"]) for row in rows})
        return resolved

    def _index_episode_terms(self, episode_id: str, *values: object) -> None:
        episode_key = self._episode_recall_key(episode_id)
        term_ids = self._recall_term_ids(self._recall_terms(*values), create=True)
        self._db.executemany(
            """INSERT OR IGNORE INTO episode_recall_terms
               (episode_key, term_id) VALUES (?, ?)""",
            ((episode_key, term_id) for term_id in term_ids.values()),
        )

    def _index_episode_message_terms(
        self,
        episode_id: str,
        message_id: int,
        content: str,
        terms: set[str] | None = None,
    ) -> None:
        terms = self._recall_terms(content) if terms is None else terms
        episode_key = self._episode_recall_key(episode_id)
        term_ids = self._recall_term_ids(terms, create=True)
        self._db.executemany(
            """INSERT OR IGNORE INTO episode_message_recall_terms
               (episode_key, message_id, term_id) VALUES (?, ?, ?)""",
            (
                (episode_key, message_id, term_id)
                for term_id in term_ids.values()
            ),
        )

    def _index_turn_episode_terms(self, turn_id: str) -> None:
        messages = self._db.execute(
            """SELECT id, role, content, delivery_state FROM messages
               WHERE turn_id=?""",
            (turn_id,),
        ).fetchall()
        plan_row = self._db.execute(
            """SELECT plan_json FROM context_plans
               WHERE turn_id=? AND state<>'superseded'
               ORDER BY revision DESC LIMIT 1""",
            (turn_id,),
        ).fetchone()
        plan = (
            normalize_context_plan(json.loads(str(plan_row["plan_json"])))
            if plan_row
            else {}
        )
        units = {
            str(unit["id"]): unit
            for unit in plan.get("intent_units", [])
            if isinstance(unit, dict) and unit.get("id")
        }
        for episode in self._db.execute(
            """SELECT episode_id, relation, unit_ids_json FROM episode_turns
               WHERE turn_id=?""",
            (turn_id,),
        ).fetchall():
            episode_id = str(episode["episode_id"])
            unit_values = [
                units[unit_id]
                for unit_id in json.loads(str(episode["unit_ids_json"]))
                if unit_id in units
            ]
            unit_terms = self._recall_terms(
                *(
                    value
                    for unit in unit_values
                    for value in (
                        unit.get("text"),
                        unit.get("intent"),
                        unit.get("references"),
                        [
                            text
                            for query in unit.get("recall_queries") or []
                            for text in recall_query_texts(query)
                        ],
                    )
                )
            )
            episode_key = self._episode_recall_key(episode_id)
            self._db.execute(
                """DELETE FROM episode_message_recall_terms
                   WHERE episode_key=? AND message_id IN (
                       SELECT id FROM messages WHERE turn_id=?
                   )""",
                (episode_key, turn_id),
            )
            if unit_terms:
                self._index_episode_terms(episode_id, *unit_values)
            for message in messages:
                if message["role"] == "assistant" and message["delivery_state"] not in {
                    "delivered",
                    "uncertain",
                    "internal",
                }:
                    continue
                content = str(message["content"])
                content_terms = self._recall_terms(content)
                if not unit_terms:
                    indexed_terms = content_terms
                elif message["role"] == "user":
                    indexed_terms = unit_terms
                elif content_terms & unit_terms or episode["relation"] == "primary":
                    indexed_terms = content_terms
                else:
                    continue
                self._index_episode_terms(episode_id, *indexed_terms)
                self._index_episode_message_terms(
                    episode_id, int(message["id"]), content, indexed_terms
                )

    def _reindex_episode_terms(self, episode_id: str) -> None:
        episode = self.episode(episode_id)
        if episode is None:
            return
        episode_key = self._episode_recall_key(episode_id)
        self._db.execute(
            "DELETE FROM episode_recall_terms WHERE episode_key=?", (episode_key,)
        )
        self._db.execute(
            "DELETE FROM episode_message_recall_terms WHERE episode_key=?",
            (episode_key,),
        )
        self._index_episode_terms(
            episode_id,
            episode["title"],
            episode["working_summary"],
            episode["narrative_summary"],
            episode["emotional_context"],
            episode["outcomes"],
            episode["topics"],
            episode["entities"],
            episode["open_loops"],
        )
        turns = self._db.execute(
            "SELECT turn_id FROM episode_turns WHERE episode_id=? ORDER BY ordinal",
            (episode_id,),
        ).fetchall()
        for turn in turns:
            self._index_turn_episode_terms(str(turn["turn_id"]))

    def _archive_day(self, timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, self._timezone).date().isoformat()

    def _ensure_runtime_archive(
        self,
        *,
        archive_kind: str,
        archive_day: str,
        episode_key: str,
        turn_id: str,
        title: str,
        now: float,
        recall_values: tuple[object, ...] = (),
    ) -> str:
        title = f"{title} · {archive_day}"
        episode_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"momoi:autonomous-episode:{episode_key}"
        ).hex
        self._db.execute(
            """INSERT OR IGNORE INTO turns
               (id, kind, workflow_kind, source_ids_json, state,
                started_at, updated_at)
               VALUES (?, 'autonomous', ?, ?, 'running', ?, ?)""",
            (turn_id, archive_kind, json.dumps([episode_key]), now, now),
        )
        self._db.execute(
            """INSERT OR IGNORE INTO conversation_episodes
               (id, title, salience, created_at, updated_at, archive_kind, archive_day)
               VALUES (?, ?, 0.4, ?, ?, ?, ?)""",
            (episode_id, title[:200], now, now, archive_kind, archive_day),
        )
        visited_successors: set[str] = set()
        while episode_id not in visited_successors:
            visited_successors.add(episode_id)
            successor = self._db.execute(
                """SELECT l.from_episode_id FROM episode_links AS l
                   JOIN conversation_episodes AS e ON e.id=l.from_episode_id
                   WHERE l.to_episode_id=? AND l.kind='continues'
                   ORDER BY e.created_at DESC LIMIT 1""",
                (episode_id,),
            ).fetchone()
            if successor is None:
                break
            successor_id = str(successor["from_episode_id"])
            if successor_id in visited_successors:
                log_event(
                    logger,
                    logging.ERROR,
                    "episode_continuation_cycle",
                    stage="storage",
                    episode_id=episode_id,
                    successor_episode_id=successor_id,
                )
                break
            episode_id = successor_id
        current = self._db.execute(
            "SELECT status FROM conversation_episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if current is not None and current["status"] == "closed":
            predecessor = episode_id
            episode_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"momoi:autonomous-successor:{predecessor}:{turn_id}",
            ).hex
            self._db.execute(
                """INSERT OR IGNORE INTO conversation_episodes
                   (id, title, salience, created_at, updated_at, archive_kind, archive_day)
                   VALUES (?, ?, 0.4, ?, ?, ?, ?)""",
                (episode_id, title[:200], now, now, archive_kind, archive_day),
            )
            self._db.execute(
                """INSERT OR IGNORE INTO episode_links
                   (from_episode_id, to_episode_id, kind)
                   VALUES (?, ?, 'continues')""",
                (episode_id, predecessor),
            )
        linked = self._db.execute(
            """SELECT 1 FROM episode_turns
               WHERE episode_id=? AND turn_id=?""",
            (episode_id, turn_id),
        ).fetchone()
        if linked is None:
            episode_id = self._roll_episode(
                episode_id,
                turn_id,
                now,
                json.dumps(recall_values, ensure_ascii=False),
            )
            ordinal = self._db.execute(
                """SELECT COALESCE(MAX(ordinal), 0) + 1 FROM episode_turns
                   WHERE episode_id=?""",
                (episode_id,),
            ).fetchone()[0]
            self._db.execute(
                """INSERT INTO episode_turns
                   (episode_id, turn_id, ordinal, relation, unit_ids_json)
                   VALUES (?, ?, ?, 'primary', '[]')""",
                (episode_id, turn_id, ordinal),
            )
        self._db.execute(
            """UPDATE conversation_episodes
               SET updated_at=?, archive_kind=?, archive_day=? WHERE id=?""",
            (now, archive_kind, archive_day, episode_id),
        )
        self._index_episode_terms(episode_id, title, *recall_values)
        self._index_turn_episode_terms(turn_id)
        return episode_id

    def _runtime_archive_kind(self, episode_id: str) -> str | None:
        """Return explicit archive ownership or classify an unmigrated row."""
        row = self._db.execute(
            f"""SELECT {runtime_archive_kind_sql('episode')} AS kind
                FROM conversation_episodes AS episode WHERE episode.id=?""",
            (episode_id,),
        ).fetchone()
        return str(row["kind"]) if row is not None and row["kind"] else None

    def create_episode(
        self,
        title: str,
        *,
        episode_id: str | None = None,
        topics: list[object] | None = None,
        entities: list[object] | None = None,
        open_loops: list[object] | None = None,
        salience: float = 0.5,
    ) -> dict[str, object]:
        title = title.strip()
        if not title:
            raise ValueError("episode title is required")
        if not 0 <= salience <= 1:
            raise ValueError("episode salience must be between 0 and 1")
        episode_id = episode_id or uuid.uuid4().hex
        now = time.time()
        with self._db:
            self._db.execute(
                """INSERT INTO conversation_episodes
                   (id, title, topics_json, entities_json, open_loops_json,
                    salience, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    episode_id,
                    title[:200],
                    json.dumps(topics or [], ensure_ascii=False, separators=(",", ":")),
                    json.dumps(
                        entities or [], ensure_ascii=False, separators=(",", ":")
                    ),
                    json.dumps(
                        open_loops or [], ensure_ascii=False, separators=(",", ":")
                    ),
                    salience,
                    now,
                    now,
                ),
            )
            self._index_episode_terms(
                episode_id, title, topics or [], entities or [], open_loops or []
            )
        saved = self.episode(episode_id)
        if saved is None:
            raise RuntimeError("episode was not saved")
        return saved

    def episode(self, episode_id: str) -> dict[str, object] | None:
        row = self._db.execute(
            "SELECT * FROM conversation_episodes WHERE id=?", (episode_id,)
        ).fetchone()
        return self._episode_dict(row) if row else None

    def transcript_window_turn_limit(
        self, minimum_turns: int, maximum_turns: int
    ) -> int:
        minimum_turns = max(1, minimum_turns)
        maximum_turns = max(minimum_turns, maximum_turns)
        latest = self._db.execute(
            """SELECT t.id, t.updated_at FROM turns AS t
               WHERE t.state='completed' AND EXISTS (
                   SELECT 1 FROM messages AS m
                   WHERE m.turn_id=t.id
                     AND (
                         m.role='user'
                         OR m.role='assistant'
                            AND m.delivery_state IN ('delivered', 'uncertain')
                     )
               )
               ORDER BY t.updated_at DESC, t.id DESC LIMIT 1"""
        ).fetchone()
        if latest is None:
            return minimum_turns
        with self._db:
            state = self._db.execute(
                "SELECT * FROM transcript_window_state WHERE id=1"
            ).fetchone()
            if state is None:
                self._db.execute(
                    """INSERT INTO transcript_window_state
                       (id, current_turns, observed_turn_id, observed_updated_at)
                       VALUES (1, ?, ?, ?)""",
                    (minimum_turns, latest["id"], latest["updated_at"]),
                )
                return minimum_turns
            new_turns = int(
                self._db.execute(
                    """SELECT COUNT(*) FROM turns AS t
                       WHERE t.state='completed'
                         AND (
                             t.updated_at>?
                             OR t.updated_at=? AND t.id>?
                         )
                         AND EXISTS (
                             SELECT 1 FROM messages AS m
                             WHERE m.turn_id=t.id
                               AND (
                                   m.role='user'
                                   OR m.role='assistant'
                                      AND m.delivery_state IN (
                                          'delivered', 'uncertain'
                                      )
                               )
                         )""",
                    (
                        state["observed_updated_at"],
                        state["observed_updated_at"],
                        state["observed_turn_id"],
                    ),
                ).fetchone()[0]
            )
            current = min(
                maximum_turns,
                max(minimum_turns, int(state["current_turns"])),
            )
            span = maximum_turns - minimum_turns
            compacted = span > 0 and current - minimum_turns + new_turns >= span
            current = (
                minimum_turns
                if span == 0
                else minimum_turns + (current - minimum_turns + new_turns) % span
            )
            self._db.execute(
                """UPDATE transcript_window_state
                   SET current_turns=?, observed_turn_id=?, observed_updated_at=?
                   WHERE id=1""",
                (current, latest["id"], latest["updated_at"]),
            )
        if compacted:
            log_event(
                logger,
                logging.INFO,
                "transcript_window_compacted",
                retained_turns=current,
                minimum_turns=minimum_turns,
                maximum_turns=maximum_turns,
                observed_new_turns=new_turns,
            )
        return current

    def recent_conversation_messages(
        self,
        turn_limit: int,
        token_budget: int,
        before_timestamp: float | None = None,
    ) -> list[dict[str, object]]:
        if turn_limit <= 0 or token_budget <= 0:
            return []
        turns = self._db.execute(
            """SELECT t.id, t.updated_at FROM turns AS t
               WHERE t.state='completed' AND EXISTS (
                   SELECT 1 FROM messages AS m
                   WHERE m.turn_id=t.id
                     AND (
                         m.role='user'
                         OR m.role='assistant'
                            AND m.delivery_state IN ('delivered', 'uncertain')
                     )
               )
                 AND (? IS NULL OR t.updated_at < ?)
               ORDER BY t.updated_at DESC LIMIT ?""",
            (before_timestamp, before_timestamp, turn_limit),
        ).fetchall()
        if not turns:
            return []
        turn_ids = [str(row["id"]) for row in turns]
        placeholders = ",".join("?" for _ in turn_ids)
        rows = self._db.execute(
            f"""SELECT m.id, m.turn_id, m.role, m.content, m.created_at,
                       m.delivery_state
                FROM messages AS m
                WHERE m.turn_id IN ({placeholders})
                  AND (m.role IN ('user', 'event') OR m.delivery_state IN ('delivered', 'uncertain'))
                ORDER BY m.id""",
            tuple(turn_ids),
        ).fetchall()
        by_turn: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            item = dict(row)
            item["timestamp"] = self.context_timestamp(item["created_at"])
            by_turn.setdefault(str(row["turn_id"]), []).append(item)
        selected: list[list[dict[str, object]]] = []
        used = 0
        for turn_id in turn_ids:
            group = by_turn.get(turn_id, [])
            if not group:
                continue
            size = sum(estimate_tokens(str(item["content"])) for item in group)
            if selected and used + size > token_budget:
                break
            if not selected and size > token_budget:
                per_message = max(1, token_budget // len(group))
                for item in group:
                    item["content"] = truncate_tokens(str(item["content"]), per_message)
                size = sum(estimate_tokens(str(item["content"])) for item in group)
            selected.append(group)
            used += size
        return [item for group in reversed(selected) for item in group]

    def turn_activity(self, turn_ids: list[str]) -> dict[str, list[dict[str, object]]]:
        """Return each Turn's work in the order it happened.

        Historical tool results are far too large to replay, but dropping them
        entirely leaves Momoi claiming actions with nothing behind them: a reply
        saying it checked a subscription reads identically whether the check
        succeeded, failed or never ran. Keeping the call, its subject, its
        outcome and any stored result reference preserves that accountability
        and still lets the exact payload be reread on demand.

        Records carry their timestamp so the caller can interleave them with the
        bubbles the same Turn delivered, which is what makes a reply readable as
        "said this, then did that, then reported the result".
        """

        ordered_ids = [str(turn_id) for turn_id in dict.fromkeys(turn_ids) if turn_id]
        if not ordered_ids:
            return {}
        placeholders = ",".join("?" for _ in ordered_ids)
        rows = self._db.execute(
            f"""SELECT turn_id, sequence, created_at, item_type, payload_json
                FROM turn_journal
                WHERE turn_id IN ({placeholders})
                  AND item_type IN ('tool_call', 'tool_result')
                ORDER BY turn_id, sequence""",
            tuple(ordered_ids),
        ).fetchall()
        outcomes: dict[str, dict[str, object]] = {}
        calls: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            call_id = str(payload.get("tool_call_id") or "")
            if str(row["item_type"]) == "tool_result":
                result = payload.get("result")
                outcomes[call_id] = {
                    "ok": bool(payload.get("ok")),
                    "error": " ".join(str(payload.get("error") or "").split())[:80],
                    "ref": str(result.get("result_ref") or "")
                    if isinstance(result, dict)
                    else "",
                }
                continue
            name = str(payload.get("name") or "")
            if not name or name in TRANSCRIPT_PROTOCOL_TOOLS:
                continue
            calls.setdefault(str(row["turn_id"]), []).append(
                {
                    "at": float(row["created_at"]),
                    "call_id": call_id,
                    "name": name,
                    "subject": _tool_call_subject(payload.get("arguments")),
                }
            )
        for records in calls.values():
            for record in records:
                record.update(
                    outcomes.get(
                        str(record.pop("call_id")), {"ok": True, "error": "", "ref": ""}
                    )
                )
        return calls

    def recent_turn_records(
        self,
        turn_limit: int,
        before_timestamp: float | None = None,
    ) -> list[dict[str, object]]:
        if turn_limit <= 0:
            return []
        workflow = turn_workflow_kind_sql("t")
        turns = self._db.execute(
            f"""SELECT t.*, {workflow} AS resolved_workflow_kind
               FROM turns AS t
               WHERE t.state<>'running'
                 AND (? IS NULL OR t.updated_at < ?)
                 AND (
                     t.kind='owner' OR EXISTS (
                         SELECT 1 FROM messages AS m
                         WHERE m.turn_id=t.id
                           AND m.role='assistant'
                           AND m.delivery_state IN ('delivered', 'uncertain')
                     )
                 )
               ORDER BY t.updated_at DESC LIMIT ?""",
            (before_timestamp, before_timestamp, turn_limit),
        ).fetchall()
        records: list[dict[str, object]] = []
        for turn in reversed(turns):
            turn_id = str(turn["id"])
            timeline: list[dict[str, object]] = []
            for message in self._db.execute(
                """SELECT id, role, content, created_at, delivery_state
                   FROM messages WHERE turn_id=? ORDER BY id""",
                (turn_id,),
            ).fetchall():
                role = str(message["role"])
                timeline.append(
                    {
                        "type": (
                            "owner_message"
                            if role == "user"
                            else ("event" if role == "event" else "assistant_message")
                        ),
                        "timestamp": self.context_timestamp(message["created_at"]),
                        "text": str(message["content"]),
                        "delivery": str(message["delivery_state"]),
                        "trust": "owner" if role == "user" else "context_data",
                        "_sort": (
                            float(message["created_at"]),
                            0,
                            int(message["id"]),
                        ),
                    }
                )
            final: dict[str, object] = {
                "state": str(turn["state"]),
                "external_effect": bool(turn["external_effect_started"]),
                "failure": str(turn["failure_reason"] or ""),
                "llm": {
                    "calls": int(turn["llm_calls"]),
                    "input_tokens": int(turn["input_tokens"]),
                    "output_tokens": int(turn["output_tokens"]),
                },
            }
            for item in self._db.execute(
                """SELECT sequence, created_at, item_type, visibility, trust,
                          payload_json
                   FROM turn_journal WHERE turn_id=?
                   ORDER BY sequence""",
                (turn_id,),
            ).fetchall():
                payload = decode_stored_json(
                    item["payload_json"],
                    entity="turn_journal",
                    record_id=f"{turn_id}:{item['sequence']}",
                    field="payload_json",
                    expected_type=dict,
                    fallback={"error": "invalid_journal_payload"},
                )
                if item["item_type"] == "final":
                    final.update(payload)
                    continue
                timeline.append(
                    {
                        "type": str(item["item_type"]),
                        "timestamp": self.context_timestamp(item["created_at"]),
                        "visibility": str(item["visibility"]),
                        "trust": str(item["trust"]),
                        **payload,
                        "_sort": (
                            float(item["created_at"]),
                            1,
                            int(item["sequence"]),
                        ),
                    }
                )
            timeline.sort(key=lambda item: item["_sort"])
            for item in timeline:
                item.pop("_sort", None)
            plan_row = self._db.execute(
                """SELECT plan_json FROM context_plans WHERE turn_id=?
                   ORDER BY revision DESC LIMIT 1""",
                (turn_id,),
            ).fetchone()
            interpretation: dict[str, object] = {}
            if plan_row is not None:
                raw_plan = decode_stored_json(
                    plan_row["plan_json"],
                    entity="context_plan",
                    record_id=turn_id,
                    field="plan_json",
                    expected_type=dict,
                    fallback={},
                )
                plan = normalize_context_plan(raw_plan)
                if isinstance(plan, dict):
                    units = plan.get("intent_units")
                    interpretation = {
                        "intents": [
                            {
                                key: unit.get(key)
                                for key in (
                                    "id",
                                    "text",
                                    "intent",
                                    "speech_act",
                                    "references",
                                )
                                if key in unit
                            }
                            for unit in units or []
                            if isinstance(unit, dict)
                        ],
                        "episode_actions": [
                            {
                                key: action.get(key)
                                for key in (
                                    "action",
                                    "episode_ref",
                                    "episode_id",
                                    "title",
                                    "unit_ids",
                                )
                                if key in action
                            }
                            for action in plan.get("episode_actions", [])
                            if isinstance(action, dict)
                        ],
                        "uncertainty": [
                            str(value) for value in plan.get("uncertainty", [])
                        ],
                    }
            records.append(
                {
                    "turn_id": turn_id,
                    "kind": str(turn["kind"]),
                    "workflow_kind": str(turn["resolved_workflow_kind"] or ""),
                    "state": str(turn["state"]),
                    "channel": str(final.get("channel") or ""),
                    "started_at": self.context_timestamp(turn["started_at"]),
                    "completed_at": self.context_timestamp(turn["updated_at"]),
                    "interpretation": interpretation,
                    "timeline": timeline,
                    "final": final,
                }
            )
        return records

    def recent_external_events(
        self,
        limit: int,
        lookback_seconds: float,
        before_timestamp: float | None = None,
    ) -> list[dict[str, object]]:
        """Return folded autonomous Events that never became shared dialogue."""

        if limit <= 0 or lookback_seconds <= 0:
            return []
        upper = float(before_timestamp) if before_timestamp is not None else time.time()
        rows = self._db.execute(
            """SELECT m.content, m.created_at, t.id AS turn_id,
                      t.source_ids_json, wr.workflow_id
               FROM messages AS m
               JOIN turns AS t ON t.id=m.turn_id
               LEFT JOIN webhook_steps AS ws
                 ON t.id=('webhook:' || ws.run_id || ':' || ws.step_index)
               LEFT JOIN webhook_runs AS wr ON wr.id=ws.run_id
               WHERE t.kind='autonomous'
                 AND t.state<>'running'
                 AND t.updated_at>=? AND t.updated_at<?
                 AND m.role='event'
                 AND m.created_at>=? AND m.created_at<?
                 AND NOT EXISTS (
                     SELECT 1 FROM messages AS visible
                     WHERE visible.turn_id=t.id
                       AND visible.role='assistant'
                       AND visible.delivery_state IN ('delivered', 'uncertain')
                 )
               ORDER BY m.created_at""",
            (
                upper - float(lookback_seconds),
                upper,
                upper - float(lookback_seconds),
                upper,
            ),
        ).fetchall()
        folded: dict[tuple[str, str], dict[str, object]] = {}
        for row in rows:
            content = " ".join(str(row["content"] or "").split())
            if not content:
                continue
            workflow_id = str(row["workflow_id"] or "").strip()
            if workflow_id:
                source = f"webhook:{workflow_id}"
            else:
                source_ids = decode_stored_json(
                    row["source_ids_json"] or "[]",
                    entity="turn",
                    record_id=row["turn_id"],
                    field="source_ids_json",
                    expected_type=list,
                    fallback=[],
                )
                raw_source = str(source_ids[0]) if source_ids else str(row["turn_id"])
                source = raw_source.split(":", 1)[0] or "autonomous"
            key = (source, content)
            seen_at = float(row["created_at"])
            item = folded.get(key)
            if item is None:
                folded[key] = {
                    "source": source,
                    "event": content,
                    "first_seen": seen_at,
                    "last_seen": seen_at,
                    "occurrences": 1,
                }
                continue
            item["last_seen"] = seen_at
            item["occurrences"] = int(item["occurrences"]) + 1
        selected = sorted(
            folded.values(),
            key=lambda item: (float(item["last_seen"]), str(item["source"])),
        )[-limit:]
        return selected

    def list_episode_directory(
        self,
        limit: int = 64,
        *,
        after: float | None = None,
        exclude_runtime_archives: bool = False,
    ) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        rows = self._db.execute(
            f"""SELECT e.*, COALESCE((
                       SELECT MAX(t.updated_at) FROM episode_turns AS et
                       JOIN turns AS t ON t.id=et.turn_id
                       WHERE et.episode_id=e.id
                   ), e.updated_at) AS last_activity_at
               FROM conversation_episodes AS e
               WHERE (? IS NULL OR COALESCE((
                   SELECT MAX(t.updated_at) FROM episode_turns AS et
                   JOIN turns AS t ON t.id=et.turn_id
                   WHERE et.episode_id=e.id
               ), e.updated_at)>=?)
               AND (?=0 OR {runtime_archive_kind_sql('e')} IS NULL)
               ORDER BY status='open' DESC, status='closing' DESC,
                        COALESCE((
                            SELECT MAX(t.updated_at) FROM episode_turns AS et
                            JOIN turns AS t ON t.id=et.turn_id
                            WHERE et.episode_id=e.id
                        ), e.updated_at) DESC, salience DESC LIMIT ?""",
            (after, after, int(exclude_runtime_archives), limit),
        ).fetchall()
        results = []
        for row in rows:
            episode = self._episode_dict(row)
            episode["last_activity_timestamp"] = self.context_timestamp(
                row["last_activity_at"]
            )
            results.append(episode)
        return results

    def list_recent_episode_directory(
        self, limit: int = 8, *, exclude_runtime_archives: bool = False
    ) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        rows = self._db.execute(
            f"""SELECT e.*, COALESCE((
                       SELECT MAX(t.updated_at) FROM episode_turns AS et
                       JOIN turns AS t ON t.id=et.turn_id
                       WHERE et.episode_id=e.id
                   ), e.updated_at) AS last_activity_at
               FROM conversation_episodes AS e
               WHERE ?=0 OR {runtime_archive_kind_sql('e')} IS NULL
               ORDER BY last_activity_at DESC, e.id DESC
               LIMIT ?""",
            (int(exclude_runtime_archives), limit),
        ).fetchall()
        results = []
        for row in rows:
            episode = self._episode_dict(row)
            episode["last_activity_timestamp"] = self.context_timestamp(
                row["last_activity_at"]
            )
            results.append(episode)
        return results

    def episode_directory_for_turns(
        self,
        turn_ids: list[str],
        *,
        exclude_runtime_archives: bool = False,
    ) -> list[dict[str, object]]:
        ordered_ids = [str(value) for value in dict.fromkeys(turn_ids) if value]
        if not ordered_ids:
            return []
        placeholders = ",".join("?" for _ in ordered_ids)
        rows = self._db.execute(
            f"""SELECT e.*, MAX(t.updated_at) AS last_activity_at,
                       GROUP_CONCAT(DISTINCT selected.turn_id) AS selected_turn_ids
                FROM episode_turns AS selected
                JOIN conversation_episodes AS e ON e.id=selected.episode_id
                JOIN episode_turns AS all_turns ON all_turns.episode_id=e.id
                JOIN turns AS t ON t.id=all_turns.turn_id
                WHERE selected.turn_id IN ({placeholders})
                GROUP BY e.id
                ORDER BY last_activity_at DESC, e.id DESC""",
            tuple(ordered_ids),
        ).fetchall()
        results = []
        for row in rows:
            episode_id = str(row["id"])
            if exclude_runtime_archives and self._runtime_archive_kind(episode_id):
                continue
            results.append(
                {
                    "id": episode_id,
                    "title": str(row["title"]),
                    "last_activity_timestamp": self.context_timestamp(
                        row["last_activity_at"]
                    ),
                    "turn_ids": [
                        value
                        for value in str(row["selected_turn_ids"] or "").split(",")
                        if value
                    ],
                }
            )
        return results

    def list_dashboard_conversations(
        self, limit: int = 64
    ) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        episode_rows = self._db.execute(
            """SELECT * FROM conversation_episodes
               ORDER BY updated_at DESC, id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        items = [
            {**self._episode_dict(row), "record_type": "episode"}
            for row in episode_rows
        ]
        turn_rows = self._db.execute(
            """SELECT t.id, t.updated_at, d.action, d.reason,
                      (
                          SELECT m.content FROM messages AS m
                          WHERE m.turn_id=t.id AND m.role='user'
                          ORDER BY m.id LIMIT 1
                      ) AS owner_content,
                      (
                          SELECT m.content FROM messages AS m
                          WHERE m.turn_id=t.id AND m.role='assistant'
                            AND m.delivery_state IN ('delivered', 'uncertain')
                          ORDER BY m.id DESC LIMIT 1
                      ) AS assistant_content
               FROM turns AS t
               LEFT JOIN episode_consolidation_decisions AS d ON d.turn_id=t.id
               WHERE t.state='completed'
                 AND NOT EXISTS (
                     SELECT 1 FROM episode_turns AS et WHERE et.turn_id=t.id
                 )
                 AND EXISTS (
                     SELECT 1 FROM messages AS m
                     WHERE m.turn_id=t.id
                       AND (
                           m.role='user'
                           OR m.role='assistant'
                              AND m.delivery_state IN ('delivered', 'uncertain')
                       )
                 )
               ORDER BY t.updated_at DESC, t.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        for row in turn_rows:
            owner = re.sub(
                r"^\d{4}-\d{2}-\d{2}T\S+\s+",
                "",
                str(row["owner_content"] or ""),
            ).strip()
            assistant = " ".join(str(row["assistant_content"] or "").split())
            status = str(row["action"] or "unclassified")
            items.append(
                {
                    "id": str(row["id"]),
                    "turn_id": str(row["id"]),
                    "record_type": "turn",
                    "status": status,
                    "title": (" ".join(owner.split()) or assistant or "未归类聊天")[:80],
                    "working_summary": "",
                    "summary": str(row["reason"] or assistant)[:240],
                    "topics": [],
                    "updated_at": float(row["updated_at"]),
                }
            )
        items.sort(
            key=lambda item: (float(item.get("updated_at") or 0), str(item["id"])),
            reverse=True,
        )
        return items[:limit]

    def dashboard_conversation_turn(
        self, turn_id: str
    ) -> dict[str, object] | None:
        row = self._db.execute(
            """SELECT t.id, t.updated_at, d.action, d.reason
               FROM turns AS t
               LEFT JOIN episode_consolidation_decisions AS d ON d.turn_id=t.id
               WHERE t.id=? AND t.state='completed'""",
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        messages = [
            {
                "id": int(message["id"]),
                "role": str(message["role"]),
                "content": str(message["content"]),
                "created_at": float(message["created_at"]),
                "delivery_state": str(message["delivery_state"]),
            }
            for message in self._db.execute(
                """SELECT id, role, content, created_at, delivery_state
                   FROM messages
                   WHERE turn_id=? AND role IN ('user', 'assistant')
                   ORDER BY id""",
                (turn_id,),
            ).fetchall()
        ]
        owner = next(
            (str(message["content"]) for message in messages if message["role"] == "user"),
            "",
        )
        owner = re.sub(r"^\d{4}-\d{2}-\d{2}T\S+\s+", "", owner).strip()
        status = str(row["action"] or "unclassified")
        return {
            "id": turn_id,
            "turn_id": turn_id,
            "record_type": "turn",
            "status": status,
            "title": (" ".join(owner.split()) or "未归类聊天")[:80],
            "working_summary": "",
            "summary": str(row["reason"] or ""),
            "topics": [],
            "updated_at": float(row["updated_at"]),
            "messages": messages,
            "truncated": False,
            "next_before_ordinal": None,
        }

    def open_conversation_inventory(self, limit: int = 64) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        rows = self._db.execute(
            f"""SELECT e.id, e.status, e.title, e.working_summary, e.open_loops_json,
                      e.updated_at,
                      COALESCE((
                          SELECT MAX(t.updated_at) FROM episode_turns AS et
                          JOIN turns AS t ON t.id=et.turn_id
                          WHERE et.episode_id=e.id
                      ), e.updated_at) AS last_activity_at
               FROM conversation_episodes AS e
               WHERE e.status IN ('open', 'closing')
                 AND {runtime_archive_kind_sql('e')} IS NULL
               ORDER BY e.status='open' DESC, last_activity_at DESC, e.updated_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        inventory: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["open_loops"] = decode_stored_json(
                item.pop("open_loops_json"),
                entity="conversation_episode",
                record_id=item["id"],
                field="open_loops_json",
                expected_type=list,
                fallback=[],
            )
            item["last_activity_timestamp"] = self.context_timestamp(
                item["last_activity_at"]
            )
            item["updated_timestamp"] = self.context_timestamp(item.pop("updated_at"))
            inventory.append(item)
        return inventory

    def open_conversation_inventory_context(self) -> str:
        rows = self.open_conversation_inventory()
        if not rows:
            return "No open or closing conversations are stored."
        lines = [
            "Inventory of conversations still marked open or closing. Use episode_id "
            "in conversation_actions to close a thread that is finished or expired. "
            "Leave it unchanged when it may still continue."
        ]
        for row in rows:
            summary = " ".join(str(row["working_summary"] or "").split())[:240]
            loops = json.dumps(row["open_loops"], ensure_ascii=False)
            lines.append(
                f"episode_id={row['id']} status={row['status']} title={row['title']} "
                f"last_activity={row['last_activity_timestamp']} open_loops={loops}"
                + (f" summary={summary}" if summary else "")
            )
        return "\n".join(lines)

    def apply_conversation_actions(
        self, actions: list[dict[str, object]], *, now: float
    ) -> None:
        if not actions:
            return
        for item in actions:
            if item.get("action") != "close":
                continue
            episode_id = str(item["episode_id"])
            row = self._db.execute(
                """SELECT id FROM conversation_episodes
                   WHERE id=? AND status IN ('open', 'closing')""",
                (episode_id,),
            ).fetchone()
            if row is None or self._runtime_archive_kind(episode_id):
                continue
            self._db.execute(
                """UPDATE conversation_episodes
                   SET status='closed', closed_at=?, open_loops_json='[]',
                       updated_at=?
                   WHERE id=? AND status IN ('open', 'closing')""",
                (now, now, episode_id),
            )
            self._reindex_episode_terms(episode_id)

    def _episode_search_documents(
        self,
        *,
        after: float | None = None,
        before: float | None = None,
    ) -> tuple[dict[str, sqlite3.Row], list[EpisodeSearchDocument]]:
        time_filter = after is not None or before is not None
        rows = self._db.execute(
            """SELECT e.*, COALESCE((
                       SELECT MAX(t.updated_at) FROM episode_turns AS et
                       JOIN turns AS t ON t.id=et.turn_id
                       WHERE et.episode_id=e.id
                   ), e.updated_at) AS last_activity_at
               FROM conversation_episodes AS e"""
        ).fetchall()
        rows_by_id = {str(row["id"]): row for row in rows}
        message_rows = self._db.execute(
            """SELECT et.episode_id, et.ordinal, et.relation, et.unit_ids_json,
                      m.id, m.turn_id, m.role, m.content, m.created_at,
                      m.delivery_state
               FROM episode_turns AS et
               JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE (m.role IN ('user', 'event') OR m.delivery_state IN
                      ('delivered', 'uncertain', 'internal'))
                 AND (? IS NULL OR m.created_at>=?)
                 AND (? IS NULL OR m.created_at<?)
               ORDER BY et.episode_id, et.ordinal, m.id""",
            (after, after, before, before),
        ).fetchall()
        active_plans = {
            str(row["turn_id"]): normalize_context_plan(
                json.loads(str(row["plan_json"]))
            )
            for row in self._db.execute(
                """SELECT cp.turn_id, cp.plan_json
                   FROM context_plans AS cp
                   JOIN (
                       SELECT turn_id, MAX(revision) AS revision
                       FROM context_plans
                       WHERE state<>'superseded'
                       GROUP BY turn_id
                   ) AS active
                     ON active.turn_id=cp.turn_id
                    AND active.revision=cp.revision"""
            ).fetchall()
        }
        messages_by_episode: dict[str, list[EpisodeSearchMessage]] = {}
        for message in message_rows:
            episode_id = str(message["episode_id"])
            turn_id = str(message["turn_id"])
            plan = active_plans.get(turn_id, {})
            units = {
                str(unit.get("id")): unit
                for unit in plan.get("intent_units", [])
                if isinstance(unit, dict) and unit.get("id")
            }
            unit_ids = json.loads(str(message["unit_ids_json"]))
            scoped_units = [
                units[unit_id]
                for unit_id in unit_ids
                if isinstance(unit_id, str) and unit_id in units
            ]
            scoped_text = "\n".join(
                str(value)
                for unit in scoped_units
                for value in (
                    unit.get("text"),
                    unit.get("intent"),
                    " ".join(str(item) for item in unit.get("references", [])),
                    " ".join(
                        text
                        for item in unit.get("recall_queries", [])
                        for text in recall_query_texts(item)
                    ),
                )
                if value
            )
            content = str(message["content"])
            searchable_text = content
            if scoped_text:
                if str(message["role"]) in {"user", "event"}:
                    searchable_text = scoped_text
                elif str(message["relation"]) != "primary":
                    searchable_text = ""
            messages_by_episode.setdefault(episode_id, []).append(
                EpisodeSearchMessage(
                    id=int(message["id"]),
                    turn_id=turn_id,
                    ordinal=int(message["ordinal"]),
                    relation=str(message["relation"]),
                    role=str(message["role"]),
                    content=content,
                    created_at=float(message["created_at"]),
                    delivery_state=str(message["delivery_state"]),
                    timestamp=self.context_timestamp(message["created_at"]),
                    searchable_text=searchable_text,
                    scoped=bool(scoped_text),
                )
            )
        documents: list[EpisodeSearchDocument] = []
        for episode_id, row in rows_by_id.items():
            messages = tuple(messages_by_episode.get(episode_id, []))
            if time_filter and not messages:
                continue
            fields = (
                ()
                if time_filter
                else (
                    EpisodeSearchField("title", str(row["title"] or "")),
                    EpisodeSearchField(
                        "working_summary", str(row["working_summary"] or "")
                    ),
                    EpisodeSearchField(
                        "summary",
                        (
                            str(row["summary"] or "")
                            if "summary" in row.keys()
                            else ""
                        ),
                    ),
                    EpisodeSearchField(
                        "narrative_summary",
                        str(row["narrative_summary"] or ""),
                    ),
                    *(
                        EpisodeSearchField("topic", str(value))
                        for value in json.loads(str(row["topics_json"] or "[]"))
                    ),
                    *(
                        EpisodeSearchField("entity", str(value))
                        for value in json.loads(str(row["entities_json"] or "[]"))
                    ),
                    *(
                        EpisodeSearchField("open_loop", str(value))
                        for value in json.loads(str(row["open_loops_json"] or "[]"))
                    ),
                )
            )
            documents.append(
                EpisodeSearchDocument(
                    episode_id=episode_id,
                    fields=fields,
                    last_activity_at=(
                        max(message.created_at for message in messages)
                        if time_filter
                        else float(row["last_activity_at"])
                    ),
                    salience=float(row["salience"]),
                    messages=messages,
                ),
            )
        return rows_by_id, documents

    def _ranked_episode_results(
        self,
        queries: list[EpisodeRecallQuery],
        max_results: int,
        *,
        after: float | None = None,
        before: float | None = None,
        offset: int = 0,
        minimum_confidence: float | None = None,
        dense_evidence: DenseRecallEvidence | None = None,
    ) -> list[dict[str, object]]:
        if max_results <= 0 or offset < 0 or not queries:
            return []
        rows_by_id, documents = self._episode_search_documents(
            after=after,
            before=before,
        )
        matches = self._episode_query.match_many(
            [query.expression for query in queries],
            documents,
        )
        hits = rank_episode_matches(
            queries,
            matches,
            documents,
            limit=max_results,
            offset=offset,
            **(
                {"minimum_confidence": minimum_confidence}
                if minimum_confidence is not None
                else {}
            ),
            dense_evidence=dense_evidence,
        )
        results: list[dict[str, object]] = []
        for hit in hits:
            row = rows_by_id.get(hit.episode_id)
            if row is None:
                continue
            episode = self._episode_dict(row)
            episode["last_activity_at"] = hit.last_activity_at
            episode["last_activity_timestamp"] = self.context_timestamp(
                hit.last_activity_at
            )
            episode["matches"] = [
                {
                    key: getattr(match, key)
                    for key in (
                        "id",
                        "turn_id",
                        "ordinal",
                        "relation",
                        "role",
                        "created_at",
                        "delivery_state",
                        "timestamp",
                    )
                }
                | {"content": truncate_tokens(match.content, 500)}
                for match in hit.matches
            ]
            episode["matched_keywords"] = list(hit.matched_keywords)
            episode["keyword_match_count"] = len(hit.matched_keywords)
            episode["search_score"] = hit.score
            episode["semantic_score"] = hit.semantic_score
            episode["relevance_confidence"] = hit.relevance_confidence
            episode["channels"] = list(hit.channels)
            episode["dense_cosine"] = hit.dense_cosine
            episode["agreement_bonus"] = hit.agreement_bonus
            episode["corroboration_bonus"] = hit.corroboration_bonus
            episode["dense_only"] = hit.dense_only
            episode["matched_queries"] = [
                {
                    "expression": query.expression,
                    "unit_ids": list(query.unit_ids),
                    "priority": query.priority,
                    "score": query.score,
                    "matched_alternatives": list(query.matched_alternatives),
                    "alternative_count": query.alternative_count,
                    "field_matches": list(query.field_matches),
                    "message_ids": list(query.message_ids),
                    "scoped_message_ids": list(query.scoped_message_ids),
                    "turn_ids": list(query.turn_ids),
                }
                for query in hit.matched_queries
            ]
            results.append(episode)
        return results

    def search_episode_queries(
        self,
        queries: list[EpisodeRecallQuery],
        max_results: int,
        *,
        after: float | None = None,
        before: float | None = None,
        offset: int = 0,
        dense_evidence: DenseRecallEvidence | None = None,
    ) -> list[dict[str, object]]:
        return self._ranked_episode_results(
            queries,
            max_results,
            after=after,
            before=before,
            offset=offset,
            dense_evidence=dense_evidence,
        )

    def search_episodes(
        self,
        query: str,
        max_results: int,
        *,
        after: float | None = None,
        before: float | None = None,
        offset: int = 0,
        dense_evidence: DenseRecallEvidence | None = None,
    ) -> list[dict[str, object]]:
        if max_results <= 0 or offset < 0:
            return []
        if query.strip():
            return self._ranked_episode_results(
                [EpisodeRecallQuery(query.strip())],
                max_results,
                after=after,
                before=before,
                offset=offset,
                minimum_confidence=0.0,
                dense_evidence=dense_evidence,
            )
        rows = self._db.execute(
            """SELECT e.*, COALESCE((
                       SELECT MAX(t.updated_at) FROM episode_turns AS et
                       JOIN turns AS t ON t.id=et.turn_id
                       WHERE et.episode_id=e.id
                   ), e.updated_at) AS last_activity_at
               FROM conversation_episodes AS e
               WHERE (? IS NULL AND ? IS NULL) OR EXISTS (
                   SELECT 1 FROM episode_turns AS et
                   JOIN messages AS m ON m.turn_id=et.turn_id
                   WHERE et.episode_id=e.id
                     AND (? IS NULL OR m.created_at>=?)
                     AND (? IS NULL OR m.created_at<?)
               )""",
            (after, before, after, after, before, before),
        ).fetchall()
        ranked = [(float(row["last_activity_at"]), row) for row in rows]
        ranked.sort(key=lambda item: item[0], reverse=True)
        results = []
        for _, row in ranked[offset : offset + max_results]:
            episode = self._episode_dict(row)
            episode["last_activity_timestamp"] = self.context_timestamp(
                row["last_activity_at"]
            )
            episode["matches"] = []
            results.append(episode)
        return results

    def conversation_message(
        self,
        episode_id: str,
        message_id: int,
        content_offset: int = 0,
        token_budget: int = 30000,
    ) -> dict[str, object] | None:
        row = self._db.execute(
            """SELECT m.id, m.turn_id, et.ordinal, m.role, m.content, m.created_at,
                      m.delivery_state
               FROM episode_turns AS et
               JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE et.episode_id=? AND m.id=?""",
            (episode_id, message_id),
        ).fetchone()
        if row is None:
            return None
        content, next_offset = token_chunk(
            str(row["content"]), content_offset, token_budget
        )
        return {
            **{
                name: row[name]
                for name in (
                    "id",
                    "turn_id",
                    "ordinal",
                    "role",
                    "created_at",
                    "delivery_state",
                )
            },
            "timestamp": self.context_timestamp(row["created_at"]),
            "content": content,
            "content_offset": content_offset,
            "next_content_offset": next_offset,
        }

    def conversation_episode(
        self,
        episode_id: str,
        token_budget: int = 30000,
        *,
        before_ordinal: int | None = None,
        after: float | None = None,
        before: float | None = None,
    ) -> dict[str, object] | None:
        episode = self.episode(episode_id)
        if episode is None:
            return None
        archived = self._db.execute(
            """SELECT et.ordinal, m.content FROM episode_turns AS et
               JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE et.episode_id=?
                 AND (? IS NULL OR et.ordinal<?)
                 AND (? IS NULL OR m.created_at>=?)
                 AND (? IS NULL OR m.created_at<?)""",
            (
                episode_id,
                before_ordinal,
                before_ordinal,
                after,
                after,
                before,
                before,
            ),
        ).fetchall()
        messages = self.episode_messages(
            episode_id,
            token_budget,
            before_ordinal=before_ordinal,
            include_nondelivered=True,
            after=after,
            before=before,
        )
        omitted_messages = len(messages) < len(archived)
        content_truncated = (
            sum(estimate_tokens(str(row["content"])) for row in archived) > token_budget
        )
        next_before_ordinal = (
            min(int(message["ordinal"]) for message in messages)
            if omitted_messages and messages
            else None
        )
        return {
            **episode,
            "messages": messages,
            "truncated": omitted_messages or content_truncated,
            "next_before_ordinal": next_before_ordinal,
            "window_first_timestamp": min(
                (str(message["timestamp"]) for message in messages),
                default=None,
            ),
            "window_last_timestamp": max(
                (str(message["timestamp"]) for message in messages),
                default=None,
            ),
        }

    def link_turn_to_episode(
        self,
        episode_id: str,
        turn_id: str,
        *,
        relation: str = "primary",
        unit_ids: list[str] | None = None,
    ) -> dict[str, object]:
        if relation not in {"primary", "related"}:
            raise ValueError("episode turn relation must be primary or related")
        archive_kind = self._runtime_archive_kind(episode_id)
        if archive_kind:
            source_kind = self.turn_workflow_kind(turn_id)
            if source_kind != archive_kind:
                raise ValueError(
                    f"{archive_kind} archive does not accept "
                    f"{source_kind or 'owner'} turns"
                )
        now = time.time()
        with self._db:
            inserted = False
            row = self._db.execute(
                """SELECT ordinal FROM episode_turns
                   WHERE episode_id=? AND turn_id=?""",
                (episode_id, turn_id),
            ).fetchone()
            if row is None:
                ordinal = int(
                    self._db.execute(
                        """SELECT COALESCE(MAX(ordinal), 0) + 1 FROM episode_turns
                           WHERE episode_id=?""",
                        (episode_id,),
                    ).fetchone()[0]
                )
                self._db.execute(
                    """INSERT INTO episode_turns
                       (episode_id, turn_id, ordinal, relation, unit_ids_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        episode_id,
                        turn_id,
                        ordinal,
                        relation,
                        json.dumps(
                            unit_ids or [], ensure_ascii=False, separators=(",", ":")
                        ),
                    ),
                )
                inserted = True
                self._db.execute(
                    """UPDATE conversation_episodes
                       SET summary_abandoned_at=NULL, summary_retry_at=NULL,
                           summary_failure_count=0
                       WHERE id=?""",
                    (episode_id,),
                )
            else:
                ordinal = int(row["ordinal"])
                self._db.execute(
                    """UPDATE episode_turns SET relation=?, unit_ids_json=?
                       WHERE episode_id=? AND turn_id=?""",
                    (
                        relation,
                        json.dumps(
                            unit_ids or [], ensure_ascii=False, separators=(",", ":")
                        ),
                        episode_id,
                        turn_id,
                    ),
                )
            self._db.execute(
                """UPDATE conversation_episodes
                   SET status=CASE WHEN ?='primary' THEN 'open' ELSE status END,
                       closed_at=CASE WHEN ?='primary' THEN NULL ELSE closed_at END,
                       updated_at=? WHERE id=?""",
                (relation, relation, now, episode_id),
            )
            if inserted and self._reorder_episode_turns(episode_id, now):
                ordinal = int(
                    self._db.execute(
                        """SELECT ordinal FROM episode_turns
                           WHERE episode_id=? AND turn_id=?""",
                        (episode_id, turn_id),
                    ).fetchone()["ordinal"]
                )
                self._reindex_episode_terms(episode_id)
            else:
                self._index_turn_episode_terms(turn_id)
        return {
            "episode_id": episode_id,
            "turn_id": turn_id,
            "ordinal": ordinal,
            "relation": relation,
            "unit_ids": unit_ids or [],
        }

    def episode_turns(self, episode_id: str) -> list[dict[str, object]]:
        rows = self._db.execute(
            "SELECT * FROM episode_turns WHERE episode_id=? ORDER BY ordinal",
            (episode_id,),
        ).fetchall()
        return [
            {
                **{key: row[key] for key in row.keys() if key != "unit_ids_json"},
                "unit_ids": json.loads(str(row["unit_ids_json"])),
            }
            for row in rows
        ]

    def episode_messages(
        self,
        episode_id: str,
        token_budget: int,
        *,
        after_ordinal: int = 0,
        before_ordinal: int | None = None,
        exclude_message_ids: set[int] | None = None,
        include_nondelivered: bool = False,
        after: float | None = None,
        before: float | None = None,
    ) -> list[dict[str, object]]:
        if token_budget <= 0:
            return []
        rows = self._db.execute(
            """SELECT m.id, m.turn_id, et.ordinal, m.role, m.content, m.created_at,
                      m.delivery_state
               FROM episode_turns AS et
               JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE et.episode_id=? AND et.ordinal>?
                 AND (? IS NULL OR et.ordinal<?)
                 AND (? IS NULL OR m.created_at>=?)
                 AND (? IS NULL OR m.created_at<?)
                 AND (? OR m.role IN ('user', 'event') OR m.delivery_state IN
                      ('delivered', 'uncertain', 'internal'))
               ORDER BY et.ordinal DESC, m.id""",
            (
                episode_id,
                after_ordinal,
                before_ordinal,
                before_ordinal,
                after,
                after,
                before,
                before,
                int(include_nondelivered),
            ),
        ).fetchall()
        excluded = exclude_message_ids or set()
        groups: list[list[dict[str, object]]] = []
        for row in rows:
            item = dict(row)
            item["timestamp"] = self.context_timestamp(item["created_at"])
            if int(item["id"]) in excluded:
                continue
            if not groups or groups[-1][0]["turn_id"] != item["turn_id"]:
                groups.append([])
            groups[-1].append(item)
        selected: list[list[dict[str, object]]] = []
        used = 0
        for group in groups:
            size = sum(estimate_tokens(str(item["content"])) for item in group)
            if selected and used + size > token_budget:
                break
            if not selected and size > token_budget:
                if len(group) > token_budget:
                    group = (
                        [group[0]]
                        if token_budget == 1
                        else [group[0], *group[-(token_budget - 1) :]]
                    )
                per_message = max(1, token_budget // len(group))
                for item in group:
                    content, next_offset = token_chunk(
                        str(item["content"]), 0, per_message
                    )
                    item["content"] = content
                    item["content_offset"] = 0
                    item["next_content_offset"] = next_offset
                size = sum(estimate_tokens(str(item["content"])) for item in group)
            selected.append(group)
            used += size
        return [item for group in reversed(selected) for item in group]













    def link_episodes(
        self, from_episode_id: str, to_episode_id: str, kind: str
    ) -> None:
        with self._db:
            if not self._insert_episode_link(
                from_episode_id, to_episode_id, kind, strict=True
            ):
                raise ValueError("invalid episode link")

    def _episode_ordering_link_creates_cycle(
        self, from_episode_id: str, to_episode_id: str
    ) -> bool:
        graph: dict[str, set[str]] = {}
        for row in self._db.execute(
            """SELECT from_episode_id, to_episode_id FROM episode_links
               WHERE kind IN ('continues', 'supersedes')"""
        ).fetchall():
            graph.setdefault(str(row["from_episode_id"]), set()).add(
                str(row["to_episode_id"])
            )
        graph.setdefault(from_episode_id, set()).add(to_episode_id)
        pending = [to_episode_id]
        visited: set[str] = set()
        while pending:
            node = pending.pop()
            if node == from_episode_id:
                return True
            if node in visited:
                continue
            visited.add(node)
            pending.extend(graph.get(node, ()))
        return False

    def _insert_episode_link(
        self,
        from_episode_id: str,
        to_episode_id: str,
        kind: str,
        *,
        strict: bool,
    ) -> bool:
        if (
            kind not in {"continues", "references", "supersedes"}
            or not from_episode_id
            or not to_episode_id
            or from_episode_id == to_episode_id
        ):
            if strict:
                raise ValueError("invalid episode link kind or endpoint")
            return False
        endpoint_count = self._db.execute(
            """SELECT COUNT(*) FROM conversation_episodes
               WHERE id IN (?, ?)""",
            (from_episode_id, to_episode_id),
        ).fetchone()[0]
        if int(endpoint_count) != 2:
            if strict:
                raise ValueError("unknown episode link endpoint")
            return False
        conflicting = self._db.execute(
            """SELECT 1 FROM episode_links
               WHERE from_episode_id=? AND to_episode_id=? AND kind<>? LIMIT 1""",
            (from_episode_id, to_episode_id, kind),
        ).fetchone()
        if conflicting is not None:
            if strict:
                raise ValueError("conflicting episode link")
            return False
        if kind in {"continues", "supersedes"} and self._episode_ordering_link_creates_cycle(
            from_episode_id, to_episode_id
        ):
            if strict:
                raise ValueError("cyclic episode link")
            return False
        self._db.execute(
            """INSERT OR IGNORE INTO episode_links
               (from_episode_id, to_episode_id, kind) VALUES (?, ?, ?)""",
            (from_episode_id, to_episode_id, kind),
        )
        return True

    @staticmethod
    def _episode_title(text: str, fallback: str) -> str:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("["):
                return line[:200]
        return fallback

    def _episode_size(self, episode_id: str) -> tuple[int, int]:
        turns = int(
            self._db.execute(
                "SELECT COUNT(*) FROM episode_turns WHERE episode_id=?",
                (episode_id,),
            ).fetchone()[0]
        )
        messages = self._db.execute(
            """SELECT m.content FROM episode_turns AS et
               JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE et.episode_id=?""",
            (episode_id,),
        ).fetchall()
        return turns, sum(
            estimate_tokens(str(message["content"])) for message in messages
        )

    def _reorder_episode_turns(self, episode_id: str, now: float) -> bool:
        rows = self._db.execute(
            """SELECT et.turn_id, et.ordinal,
                      COALESCE(MIN(m.created_at), t.started_at, t.updated_at) AS occurred_at,
                      t.started_at
               FROM episode_turns AS et
               JOIN turns AS t ON t.id=et.turn_id
               LEFT JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE et.episode_id=?
               GROUP BY et.turn_id, et.ordinal, t.started_at, t.updated_at
               ORDER BY occurred_at, t.started_at, et.turn_id""",
            (episode_id,),
        ).fetchall()
        if all(int(row["ordinal"]) == index for index, row in enumerate(rows, 1)):
            return False
        offset = max(int(row["ordinal"]) for row in rows) + len(rows) + 1
        self._db.execute(
            "UPDATE episode_turns SET ordinal=ordinal+? WHERE episode_id=?",
            (offset, episode_id),
        )
        self._db.executemany(
            """UPDATE episode_turns SET ordinal=?
               WHERE episode_id=? AND turn_id=?""",
            (
                (index, episode_id, str(row["turn_id"]))
                for index, row in enumerate(rows, 1)
            ),
        )
        self._db.execute(
            """UPDATE conversation_episodes
               SET working_summary='', working_summary_claims_json='[]',
                   narrative_summary='', emotional_context_json='{}',
                   outcomes_json='[]', summarized_through_ordinal=0,
                   summary_claimed_at=NULL, summary_retry_at=NULL,
                   summary_failure_count=0, updated_at=?
               WHERE id=?""",
            (now, episode_id),
        )
        log_event(
            logger,
            logging.INFO,
            "episode_turns_reordered",
            stage="storage",
            episode_id=episode_id,
            turns=len(rows),
        )
        return True

    @staticmethod
    def _episode_actions(plan: dict[str, object]) -> list[dict[str, object]]:
        actions = plan.get("episode_actions")
        return (
            [
                item
                for item in actions
                if isinstance(item, dict) and item.get("action") != "none"
            ]
            if isinstance(actions, list)
            else []
        )

    def _roll_episode(
        self,
        episode_id: str,
        turn_id: str,
        now: float,
        raw_text: str,
        *,
        incoming_turns: int = 1,
    ) -> str:
        turns, raw_tokens = self._episode_size(episode_id)
        if (
            turns + incoming_turns <= 64
            and raw_tokens + estimate_tokens(raw_text) < 64000
        ):
            return episode_id
        row = self._db.execute(
            "SELECT * FROM conversation_episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if row is None:
            return episode_id
        successor = uuid.uuid5(
            uuid.NAMESPACE_URL, f"momoi:episode-successor:{episode_id}:{turn_id}"
        ).hex
        self._db.execute(
            """INSERT OR IGNORE INTO conversation_episodes
               (id, status, title, topics_json, entities_json, open_loops_json,
                salience, created_at, updated_at, archive_kind, archive_day)
               VALUES (?, 'closing', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                successor,
                row["title"],
                row["topics_json"],
                row["entities_json"],
                row["open_loops_json"],
                row["salience"],
                now,
                now,
                row["archive_kind"],
                row["archive_day"],
            ),
        )
        self._db.execute(
            """UPDATE conversation_episodes
               SET status='closed', closed_at=?, updated_at=? WHERE id=?""",
            (now, now, episode_id),
        )
        self._db.execute(
            """INSERT OR IGNORE INTO episode_links
               (from_episode_id, to_episode_id, kind)
               VALUES (?, ?, 'continues')""",
            (successor, episode_id),
        )
        log_event(
            logger,
            logging.INFO,
            "episode_rolled",
            stage="storage",
            episode_id=episode_id,
            successor_episode_id=successor,
            turns=turns,
            raw_tokens=raw_tokens,
        )
        return successor

    def _apply_context_plan_episodes(
        self,
        turn_id: str,
        now: float,
        raw_text: str,
        *,
        keep_open: bool = False,
    ) -> None:
        row = self._db.execute(
            """SELECT plan_json FROM context_plans
               WHERE turn_id=? AND state<>'superseded'
               ORDER BY revision DESC LIMIT 1""",
            (turn_id,),
        ).fetchone()
        plan = (
            normalize_context_plan(json.loads(str(row["plan_json"])))
            if row is not None
            else {}
        )
        actions = self._episode_actions(plan)
        links = plan.get("episode_links", [])
        selected: set[str] = set()
        resolved: dict[str, str] = {}
        rejected: set[str] = set()
        self._db.execute("DELETE FROM episode_turns WHERE turn_id=?", (turn_id,))
        for action in actions:
            episode_id = str(action["episode_id"])
            existing = self._db.execute(
                "SELECT * FROM conversation_episodes WHERE id=?", (episode_id,)
            ).fetchone()
            archive_kind = (
                self._runtime_archive_kind(episode_id)
                if existing is not None
                else None
            )
            if archive_kind:
                rejected.add(episode_id)
                log_event(
                    logger,
                    logging.WARNING,
                    "owner_episode_binding_rejected",
                    stage="storage",
                    turn_id=turn_id,
                    episode_id=episode_id,
                    reason=f"{archive_kind}_archive",
                )
                continue
            if existing is not None:
                episode_id = self._roll_episode(
                    episode_id, turn_id, now, raw_text
                )
                existing = self._db.execute(
                    "SELECT * FROM conversation_episodes WHERE id=?", (episode_id,)
                ).fetchone()
            resolved[str(action["episode_id"])] = episode_id
            topics = list(action.get("topics") or [])
            entities = list(action.get("entities") or [])
            loops = list(action.get("open_loops") or [])
            status = "open" if loops or keep_open else "closing"
            if existing is None:
                self._db.execute(
                    """INSERT INTO conversation_episodes
                       (id, status, title, topics_json, entities_json,
                        open_loops_json, salience, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        episode_id,
                        status,
                        str(action["title"]),
                        json.dumps(topics, ensure_ascii=False),
                        json.dumps(entities, ensure_ascii=False),
                        json.dumps(loops, ensure_ascii=False),
                        float(action.get("salience", 0.5)),
                        now,
                        now,
                    ),
                )
            else:
                old_topics = json.loads(str(existing["topics_json"]))
                old_entities = json.loads(str(existing["entities_json"]))
                merged_topics = list(dict.fromkeys([*old_topics, *topics]))[:12]
                merged_entities = list(dict.fromkeys([*old_entities, *entities]))[:20]
                self._db.execute(
                    """UPDATE conversation_episodes
                       SET topics_json=?, entities_json=?, open_loops_json=?,
                           salience=MAX(salience, ?), status=?, closed_at=NULL,
                           updated_at=? WHERE id=?""",
                    (
                        json.dumps(merged_topics, ensure_ascii=False),
                        json.dumps(merged_entities, ensure_ascii=False),
                        json.dumps(loops, ensure_ascii=False),
                        float(action.get("salience", 0.5)),
                        status,
                        now,
                        episode_id,
                    ),
                )
            ordinal = int(
                self._db.execute(
                    """SELECT COALESCE(MAX(ordinal), 0) + 1
                       FROM episode_turns WHERE episode_id=?""",
                    (episode_id,),
                ).fetchone()[0]
            )
            self._db.execute(
                """INSERT INTO episode_turns
                   (episode_id, turn_id, ordinal, relation, unit_ids_json)
                   VALUES (?, ?, ?, 'primary', ?)""",
                (
                    episode_id,
                    turn_id,
                    ordinal,
                    json.dumps(action.get("unit_ids") or [], ensure_ascii=False),
                ),
            )
            selected.add(episode_id)
            self._reindex_episode_terms(episode_id)
        if selected:
            placeholders = ",".join("?" for _ in selected)
            self._db.execute(
                f"""UPDATE conversation_episodes SET status='closed',
                    closed_at=?, updated_at=?
                    WHERE status='closing' AND id NOT IN ({placeholders})
                      AND {runtime_archive_kind_sql('conversation_episodes')} IS NULL
                      AND id IN (
                          SELECT et.episode_id FROM episode_turns AS et
                          JOIN turns AS t ON t.id=et.turn_id WHERE t.kind='owner'
                      )""",
                (now, now, *selected),
            )
        elif not keep_open:
            self._db.execute(
                f"""UPDATE conversation_episodes SET status='closed',
                   closed_at=?, updated_at=?
                   WHERE status='closing'
                     AND {runtime_archive_kind_sql('conversation_episodes')} IS NULL
                     AND id IN (
                       SELECT et.episode_id FROM episode_turns AS et
                       JOIN turns AS t ON t.id=et.turn_id WHERE t.kind='owner'
                   )""",
                (now, now),
            )
        for link in links if isinstance(links, list) else []:
            if not isinstance(link, dict):
                continue
            if (
                str(link["from_episode_id"]) in rejected
                or str(link["to_episode_id"]) in rejected
            ):
                continue
            source = resolved.get(
                str(link["from_episode_id"]), str(link["from_episode_id"])
            )
            target = resolved.get(
                str(link["to_episode_id"]), str(link["to_episode_id"])
            )
            if source == target:
                continue
            kind = str(link["kind"])
            if not self._insert_episode_link(source, target, kind, strict=False):
                log_event(
                    logger,
                    logging.WARNING,
                    "episode_link_rejected",
                    stage="storage",
                    from_episode_id=source,
                    to_episode_id=target,
                    kind=kind,
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

    def _notification_key_not_before(
        self,
        priority: str,
        notification_key: str,
        config: NotificationConfig,
        now: float,
        *,
        apply_cooldown: bool = True,
    ) -> float:
        eligible = now
        if priority == "normal":
            eligible = max(eligible, quiet_until(now, self._timezone, config))
            last = self._db.execute(
                """SELECT MAX(n.queued_at) FROM notifications AS n
                   WHERE n.notification_key=? AND (
                       n.state='queued' OR EXISTS (
                           SELECT 1 FROM outbox AS o
                           WHERE o.turn_id=n.turn_id
                             AND (o.state='sent' OR o.possible_duplicate=1)
                       )
                   )""",
                (notification_key,),
            ).fetchone()[0]
            if apply_cooldown and last is not None:
                eligible = max(eligible, float(last) + config.cooldown_seconds)
            if self._db.execute(
                "SELECT 1 FROM events WHERE processed=0 LIMIT 1"
            ).fetchone():
                eligible = max(eligible, now + config.pending_owner_delay_seconds)
        return eligible

    def _notification_not_before(
        self, row: sqlite3.Row, config: NotificationConfig, now: float
    ) -> float:
        return self._notification_key_not_before(
            str(row["priority"]), str(row["notification_key"]), config, now
        )

    def heartbeat_contact_window(
        self,
        notification_key: str,
        config: NotificationConfig,
        now: float | None = None,
        *,
        apply_cooldown: bool = True,
    ) -> dict[str, object]:
        now = time.time() if now is None else now
        eligible_at = self._notification_key_not_before(
            "normal",
            notification_key,
            config,
            now,
            apply_cooldown=apply_cooldown,
        )
        return {"allowed": eligible_at <= now, "eligible_at": eligible_at}

    def claim_due_notification(
        self, config: NotificationConfig, now: float | None = None
    ) -> dict[str, object] | None:
        now = time.time() if now is None else now
        with self._db:
            row = self._db.execute(
                """SELECT * FROM notifications
                   WHERE state='pending' AND claimed_at IS NULL AND not_before<=?
                   ORDER BY not_before, created_at LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            eligible = self._notification_not_before(row, config, now)
            if eligible > now:
                self._db.execute(
                    "UPDATE notifications SET not_before=? WHERE id=?",
                    (eligible, row["id"]),
                )
                return None
            self._db.execute(
                "UPDATE notifications SET claimed_at=? WHERE id=?", (now, row["id"])
            )
        return dict(row)

    def next_notification_due_at(self) -> float | None:
        row = self._db.execute(
            """SELECT MIN(not_before) FROM notifications
               WHERE state='pending' AND claimed_at IS NULL"""
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def _queue_notification_row(
        self, row: sqlite3.Row, now: float, primary_channel: str
    ) -> None:
        messages = json.loads(str(row["messages_json"]))
        target_channel = str(row["target_channel"] or primary_channel)
        source = (
            f"heartbeat:{row['turn_id']}"
            if row["goal_id"] == "heartbeat"
            else f"goal:{row['goal_id']}"
        )
        visible_messages: list[str] = []
        for index, message in enumerate(messages):
            visible, kind, path, payload = self._outbox_content(message)
            visible_messages.append(visible)
            dedupe_key = f"notification:{row['id']}:{index}"
            outbox = self._db.execute(
                """INSERT OR IGNORE INTO outbox
                   (turn_id, dedupe_key, text, kind, media_path, payload_json,
                    reply_expectation, target_channel)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["turn_id"],
                    dedupe_key,
                    visible,
                    kind,
                    path,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    row["reply_expectation"] if index == len(messages) - 1 else "",
                    target_channel,
                ),
            )
            outbox_id = (
                int(outbox.lastrowid)
                if outbox.lastrowid
                else int(
                    self._db.execute(
                        "SELECT id FROM outbox WHERE dedupe_key=?", (dedupe_key,)
                    ).fetchone()["id"]
                )
            )
            self._db.execute(
                """INSERT INTO messages
                   (turn_id, role, content, created_at, source_event_ids_json,
                    outbox_id, delivery_state)
                   SELECT ?, 'assistant', ?, ?, ?, ?, 'queued'
                   WHERE NOT EXISTS (
                       SELECT 1 FROM messages WHERE outbox_id=?
                   )""",
                (
                    row["turn_id"],
                    visible,
                    now,
                    json.dumps([source]),
                    outbox_id,
                    outbox_id,
                ),
            )
        if visible_messages:
            archive_kind = "heartbeat" if row["goal_id"] == "heartbeat" else "goal"
            archive_time = (
                self._heartbeat_turn_time(str(row["turn_id"]), now)
                if archive_kind == "heartbeat"
                else now
            )
            archive_day = self._archive_day(archive_time)
            title = (
                "心跳"
                if archive_kind == "heartbeat"
                else self._episode_title(
                    visible_messages[0], "Autonomous conversation"
                )
            )
            self._ensure_runtime_archive(
                archive_kind=archive_kind,
                archive_day=archive_day,
                episode_key=(
                    f"heartbeat:day:{archive_day}"
                    if archive_kind == "heartbeat"
                    else f"goal:{row['goal_id']}:day:{archive_day}"
                ),
                turn_id=str(row["turn_id"]),
                title=title,
                now=now,
                recall_values=(visible_messages,),
            )
        self._db.execute(
            """UPDATE notifications SET state='queued', claimed_at=NULL, queued_at=?
               WHERE id=?""",
            (now, row["id"]),
        )

    def queue_notification(
        self,
        notification_id: str,
        now: float | None = None,
        config: NotificationConfig | None = None,
        primary_channel: str = "",
    ) -> bool:
        now = time.time() if now is None else now
        with self._db:
            row = self._db.execute(
                """SELECT * FROM notifications
                   WHERE id=? AND state='pending' AND claimed_at IS NOT NULL""",
                (notification_id,),
            ).fetchone()
            if row is None:
                return False
            if config is not None:
                eligible = self._notification_not_before(row, config, now)
                if eligible > now:
                    self._db.execute(
                        """UPDATE notifications SET claimed_at=NULL, not_before=?
                           WHERE id=?""",
                        (eligible, notification_id),
                    )
                    return False
            self._queue_notification_row(row, now, primary_channel)
        return True
