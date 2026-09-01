from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING

from ..channel import (
    ChannelMessage,
    media_path,
    normalize_channel_message,
    render_channel_message,
)
from ..config import HeartbeatConfig, NotificationConfig, ReflectionConfig
from ..context_time import context_timestamp
from ..emotions import emotion_slug, valid_emotion_slug
from ..extensions.base import UsagePlugin
from ..llm_usage import PRICING_NOTE, summarize_usage
from ..logging_context import log_event, safe_preview
from ..models import (
    AgentReply,
    IncomingMessage,
    TurnDraft,
)
from ..policies import MemoryPolicy
from ..reply_wait import encode_reply_wait
from ..search import (
    SearchBackend,
    StringSearchBackend,
    search_alternatives,
)
from .delivery import DeliveryStore
from .episode_search import (
    EpisodeQueryService,
    EpisodeSearchBackend,
    EpisodeSearchDocument,
    EpisodeSearchField,
    EpisodeSearchMessage,
    StringEpisodeSearchBackend,
)
from .episode_ranking import EpisodeRecallQuery, rank_episode_matches
from .memory import (
    MemoryStore,
    RECENT_MEMORY_WINDOW_SECONDS,
    estimate_tokens,
    memory_snapshot_fingerprint,
    token_chunk,
    truncate_tokens,
)
from .semantic import SemanticStore
from .scheduling import next_schedule_at, quiet_until
from .thinking import ThinkingStore, month_bounds, parse_month

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..semantic import DenseRecallEvidence


EPISODE_ANNEAL_MAX_FAILURES = 3


def _recall_query_texts(value: object) -> list[str]:
    if not isinstance(value, dict):
        text = " ".join(str(value or "").split())
        return [text] if text else []
    semantic = " ".join(str(value.get("semantic") or "").split())
    keywords = [
        " ".join(str(item).split())
        for item in value.get("keywords") or []
        if " ".join(str(item).split())
    ]
    return list(dict.fromkeys([semantic, *keywords])) if semantic else keywords

BASELINE_MOOD_STATE = "calm"
BASELINE_MOOD_INTENSITY = 0.35
BASELINE_MOOD_CAUSE = "resting baseline"
DEFAULT_ACTIVITY = "spending time freely"
RECENT_HEARTBEAT_LIMIT = 6
# Delivery and turn-control calls are protocol, not work worth recalling.
TRANSCRIPT_PROTOCOL_TOOLS = frozenset(
    {
        "send_message",
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
EPISODE_CONSOLIDATION_LOOKBACK_SECONDS = 30 * 24 * 60 * 60
_HEARTBEAT_RECORD_ACTIVITY = re.compile(
    r"^Activity: (.*)$", re.MULTILINE
)
REFLECTION_MEMORY_KINDS = {
    "owner_profile",
    "owner_preference",
    "world_knowledge",
    "self_insight",
    "relationship",
    "shared_experience",
    "practice",
    "tool_skill",
}
def _group_thinking_turns(calls: list[dict[str, object]]) -> list[dict[str, object]]:
    buckets: dict[str, list[dict[str, object]]] = {}
    order: list[str] = []
    for call in calls:
        key = str(call.get("turn_id") or "") or f"call:{call.get('call_id') or ''}"
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(call)
    turns: list[dict[str, object]] = []
    for key in order:
        items = sorted(
            buckets[key],
            key=lambda item: (float(item.get("created_at") or 0), int(item.get("round") or 0)),
        )
        stages: list[str] = []
        tools: list[str] = []
        for item in items:
            stage = str(item.get("stage") or "")
            if stage not in stages:
                stages.append(stage)
            for tool in item.get("tools") or []:
                name = str(tool or "")
                if name and name not in tools:
                    tools.append(name)
        turns.append(
            {
                "id": key,
                "turn_id": str(items[0].get("turn_id") or ""),
                "created_at": items[0].get("created_at"),
                "updated_at": items[-1].get("created_at"),
                "call_count": len(items),
                "stages": stages,
                "tools": tools,
                "excerpt": str(items[0].get("excerpt") or ""),
                "reasoning_chars": sum(
                    int(item.get("reasoning_chars") or 0) for item in items
                ),
            }
        )
    turns.sort(
        key=lambda item: (-float(item.get("updated_at") or 0), str(item.get("id") or ""))
    )
    return turns


def _add_context_timestamps(
    value: dict[str, object], fields: tuple[str, ...]
) -> None:
    for name in fields:
        if value.get(name) is not None:
            value[f"{name.removesuffix('_at')}_timestamp"] = context_timestamp(
                value[name]
            )


def _owner_message_created_at(
    events: list[IncomingMessage], now: float
) -> float:
    times = [
        float(event.occurred_at or event.received_at)
        for event in events
        if event.occurred_at or event.received_at
    ]
    return min(times) if times else now


def _heartbeat_record_activity(content: str) -> str:
    match = _HEARTBEAT_RECORD_ACTIVITY.search(content)
    return match.group(1).strip()[:300] if match else ""


def _reflection_json(value: object, fallback: object) -> object:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback
    return parsed


def _reflection_compact_value(value: object, limit: int = 240) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return " ".join(str(value or "").split())[:limit]


def _reflection_select_entries(
    entries: list[tuple[float, str, str, bool, bool]], token_budget: int
) -> list[tuple[float, str, str, bool, bool]]:
    """Keep a day-wide shape instead of selecting only the latest records."""
    if not entries or token_budget <= 0:
        return []
    ordered = sorted(entries)
    total = sum(estimate_tokens(f"[{label}]\n{content}") for _, label, content, _, _ in ordered)
    if total <= token_budget:
        return ordered
    selected: list[tuple[float, str, str, bool, bool]] = []
    selected_ids: set[int] = set()
    used = 0

    def add(index: int) -> None:
        nonlocal used
        if index in selected_ids:
            return
        entry = ordered[index]
        size = estimate_tokens(f"[{entry[1]}]\n{entry[2]}")
        if used + size > token_budget:
            return
        selected_ids.add(index)
        selected.append(entry)
        used += size

    head = max(1, len(ordered) // 5)
    tail = max(1, len(ordered) // 2)
    for index in range(head):
        add(index)
    for index in range(max(0, len(ordered) - tail), len(ordered)):
        add(index)
    for index, entry in enumerate(ordered):
        if entry[1] in {"OWNER", "EVENT", "RUNTIME FAILURE"} or entry[1].startswith("TOOL "):
            add(index)
    if not selected:
        entry = ordered[-1]
        selected = [
            (*entry[:2], truncate_tokens(entry[2], max(1, token_budget - 4)), *entry[3:])
        ]
    return sorted(selected)


def _dashboard_unix(value: object) -> float | None:
    if value is None:
        return None
    stamp = float(value)
    return stamp if stamp > 0 else None


class Store(MemoryStore, DeliveryStore, SemanticStore):
    def __init__(
        self,
        path: Path,
        workspace: Path | None = None,
        memory_policy: MemoryPolicy = MemoryPolicy(),
        search_backend: SearchBackend | None = None,
        episode_search_backend: EpisodeSearchBackend | None = None,
        thinking: Path | None = None,
    ) -> None:
        database = Path(path).expanduser().resolve()
        self._workspace = (workspace or database.parent).expanduser().resolve()
        self._memory_policy = memory_policy
        self._search_backend = search_backend or StringSearchBackend()
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
        now = time.time()
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

    def add_event(self, message: IncomingMessage) -> bool:
        payload = {
            "channel": message.channel,
            "segments": message.segments,
        }
        with self._db:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO events
                   (id, message_id, kind, content, occurred_at, received_at, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    message.event_id,
                    message.message_id,
                    f"{message.channel}.message",
                    message.text,
                    message.occurred_at,
                    message.received_at,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            if cursor.rowcount == 1:
                now = time.time()
                self._cool_active_reply(now, "owner_message_received")
                self._db.execute(
                    """UPDATE self_state SET pending_reply_turn_id=NULL,
                       pending_reply_expectation='', pending_reply_since=NULL,
                       pending_reply_checks=0, pending_reply_last_reason='',
                       pending_reply_channel='',
                       pending_reply_delay_minutes=0,
                       pending_reply_next_check_at=NULL,
                       updated_at=? WHERE id=1""",
                    (now,),
                )
                self._supersede_heartbeat_contacts(
                    ("heartbeat.chat", "heartbeat.reply_followup"),
                    "owner_message_superseded_heartbeat_contact",
                    now,
                )
        return cursor.rowcount == 1

    def _cool_active_reply(self, now: float, _reason: str) -> bool:
        row = self._db.execute(
            """SELECT pending_reply_turn_id, pending_reply_expectation,
                      pending_reply_since, pending_reply_last_reason,
                      pending_reply_delay_minutes, pending_reply_next_check_at
               FROM self_state WHERE id=1"""
        ).fetchone()
        expectation = str(row["pending_reply_expectation"] or "").strip() if row else ""
        if not expectation:
            return False
        self._db.execute(
            """UPDATE self_state SET cooled_reply_expectation=?,
                   cooled_reply_source_turn_id=?, cooled_reply_since=?,
                   cooled_reply_due_at=?, cooled_reply_delay_minutes=?,
                   cooled_reply_waiting_since=?, cooled_reply_review_at=NULL,
                   cooled_reply_checks=0,
                   cooled_reply_reason=?, updated_at=? WHERE id=1""",
            (
                expectation,
                str(row["pending_reply_turn_id"] or ""),
                now,
                row["pending_reply_next_check_at"],
                int(row["pending_reply_delay_minutes"] or 0),
                row["pending_reply_since"],
                str(row["pending_reply_last_reason"] or "")[:500],
                now,
            ),
        )
        self._release_reply_episode_hold(
            str(row["pending_reply_turn_id"] or ""),
            now,
        )
        return True

    def _release_reply_episode_hold(self, turn_id: str, now: float) -> None:
        if not turn_id:
            return
        self._db.execute(
            """UPDATE conversation_episodes
               SET status='closing', closed_at=NULL, updated_at=?
               WHERE status='open' AND open_loops_json='[]'
                 AND id IN (
                     SELECT episode_id FROM episode_turns WHERE turn_id=?
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM episode_turns AS archive_turn
                     WHERE archive_turn.episode_id=conversation_episodes.id
                       AND (
                           archive_turn.turn_id GLOB 'webhook:*'
                           OR EXISTS (
                               SELECT 1 FROM turns AS archive_source
                               WHERE archive_source.id=archive_turn.turn_id
                                 AND archive_source.kind='autonomous'
                                 AND EXISTS (
                                     SELECT 1
                                     FROM json_each(
                                         archive_source.source_ids_json
                                     ) AS source_id
                                     WHERE source_id.value GLOB 'heartbeat:*'
                                 )
                           )
                       )
                 )""",
            (now, turn_id),
        )

    def cooled_reply_expectation_context(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        row = self._db.execute(
            """SELECT cooled_reply_expectation, cooled_reply_source_turn_id,
                      cooled_reply_since, cooled_reply_due_at,
                      cooled_reply_delay_minutes, cooled_reply_waiting_since,
                      cooled_reply_reason
               FROM self_state WHERE id=1"""
        ).fetchone()
        expectation = str(row["cooled_reply_expectation"] or "").strip() if row else ""
        if not expectation:
            return ""
        source_turn = str(row["cooled_reply_source_turn_id"] or "")
        source_rows = self._db.execute(
            """SELECT role, content, created_at, delivery_state FROM messages
               WHERE turn_id=? AND (role IN ('user', 'event') OR delivery_state IN ('delivered','uncertain'))
               ORDER BY id""",
            (source_turn,),
        ).fetchall()
        source_messages = [
            {
                "role": str(item["role"]),
                "content": str(item["content"]),
                "delivery_state": str(item["delivery_state"]),
                "timestamp": context_timestamp(item["created_at"]),
            }
            for item in source_rows
        ]
        return json.dumps(
            {
                "state": "owner_replied_before_deadline",
                "expected_information": expectation,
                "reason": str(row["cooled_reply_reason"] or ""),
                "source_turn": source_turn,
                "source_messages": source_messages,
                "waiting_since": context_timestamp(
                    row["cooled_reply_waiting_since"] or now
                ),
                "interrupted_at": context_timestamp(row["cooled_reply_since"] or now),
                "deadline": context_timestamp(row["cooled_reply_due_at"] or now),
                "delay_minutes": int(row["cooled_reply_delay_minutes"] or 0),
                "elapsed_minutes": max(
                    0,
                    int(
                        (
                            float(row["cooled_reply_since"] or now)
                            - float(row["cooled_reply_waiting_since"] or now)
                        )
                        / 60
                    ),
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _apply_cooled_reply_action(
        self, _draft: TurnDraft | None, now: float
    ) -> None:
        self._db.execute(
            """UPDATE self_state SET cooled_reply_expectation='',
               cooled_reply_source_turn_id='', cooled_reply_since=NULL,
               cooled_reply_due_at=NULL, cooled_reply_delay_minutes=0,
               cooled_reply_waiting_since=NULL, cooled_reply_review_at=NULL,
               cooled_reply_checks=0, cooled_reply_reason='', updated_at=?
               WHERE id=1 AND cooled_reply_expectation<>''""",
            (now,),
        )

    def _supersede_heartbeat_contacts(
        self, keys: tuple[str, ...], reason: str, now: float
    ) -> None:
        placeholders = ",".join("?" for _ in keys)
        stale = self._db.execute(
            f"""SELECT o.id, o.state FROM outbox AS o
                LEFT JOIN notifications AS n ON n.turn_id=o.turn_id
                WHERE (
                    n.notification_key IN ({placeholders}) OR EXISTS (
                        SELECT 1 FROM turns AS t
                        WHERE t.id=o.turn_id
                          AND t.kind='autonomous'
                          AND t.state='running'
                          AND (
                              t.source_ids_json LIKE '%\"heartbeat:%'
                              OR t.source_ids_json LIKE '%\"reply-followup:%'
                          )
                    )
                ) AND o.state IN ('pending', 'ambiguous')""",
            keys,
        ).fetchall()
        for row in stale:
            outbox_id = int(row["id"])
            episodes = self._db.execute(
                """SELECT DISTINCT et.episode_id FROM messages AS m
                   JOIN episode_turns AS et ON et.turn_id=m.turn_id
                   WHERE m.outbox_id=?""",
                (outbox_id,),
            ).fetchall()
            self._db.execute(
                "UPDATE outbox SET state='superseded', last_error=? WHERE id=?",
                (reason, outbox_id),
            )
            if row["state"] == "ambiguous":
                self._sync_outbox_message(outbox_id, "ambiguous")
            else:
                self._db.execute("DELETE FROM messages WHERE outbox_id=?", (outbox_id,))
                for episode in episodes:
                    self._reindex_episode_terms(str(episode["episode_id"]))
        self._db.execute(
            f"""UPDATE notifications AS n
                SET state='superseded', claimed_at=NULL,
                    superseded_at=?, superseded_reason=?
                WHERE notification_key IN ({placeholders})
                  AND state IN ('pending', 'queued')
                  AND (
                      state='pending' OR EXISTS (
                          SELECT 1 FROM outbox AS o
                          WHERE o.turn_id=n.turn_id AND o.state='superseded'
                      )
                  )""",
            (now, reason, *keys),
        )

    def pending_events(self) -> list[IncomingMessage]:
        rows = self._db.execute(
            "SELECT * FROM events WHERE processed=0 ORDER BY received_at, rowid"
        ).fetchall()
        return [self._incoming_message(row) for row in rows]

    def recent_owner_events(self, limit: int = 20) -> list[IncomingMessage]:
        rows = self._db.execute(
            """SELECT * FROM events
               WHERE processed=1 AND content NOT LIKE '/%'
               ORDER BY received_at DESC, rowid DESC LIMIT ?""",
            (max(0, limit),),
        ).fetchall()
        return [self._incoming_message(row) for row in reversed(rows)]

    def heartbeat_conversation_snapshot(self) -> dict[str, object]:
        revision = int(
            self._db.execute("SELECT COALESCE(MAX(rowid), 0) FROM events").fetchone()[0]
        )
        if self._db.execute(
            "SELECT 1 FROM events WHERE processed=0 LIMIT 1"
        ).fetchone():
            blocked_by = "pending_owner_event"
        elif self._db.execute(
            """SELECT 1 FROM outbox AS o
               JOIN turns AS t ON t.id=o.turn_id
               WHERE t.kind='owner'
                 AND o.state IN ('pending', 'sending', 'ambiguous') LIMIT 1"""
        ).fetchone():
            blocked_by = "owner_reply_in_flight"
        else:
            blocked_by = ""
        return {
            "owner_event_revision": revision,
            "owner_busy": bool(blocked_by),
            "blocked_by": blocked_by,
        }

    @staticmethod
    def _incoming_message(row: sqlite3.Row) -> IncomingMessage:
        raw = str(row["payload_json"] or "")
        try:
            value = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            value = []
        if isinstance(value, dict):
            channel = str(value.get("channel") or "unknown")
            raw_segments = value.get("segments") or []
        else:
            kind = str(row["kind"] or "")
            channel = kind.removesuffix(".message") or "unknown"
            raw_segments = value if isinstance(value, list) else []
        segments = tuple(item for item in raw_segments if isinstance(item, dict))
        if not segments and row["content"]:
            segments = ({"type": "text", "data": {"text": row["content"]}},)
        return IncomingMessage(
            event_id=row["id"],
            message_id=row["message_id"],
            text=row["content"],
            occurred_at=row["occurred_at"],
            received_at=row["received_at"],
            segments=segments,
            channel=channel,
        )

    def discard_events(self, events: list[IncomingMessage]) -> None:
        with self._db:
            self._db.executemany(
                "UPDATE events SET processed=1 WHERE id=?",
                ((event.event_id,) for event in events),
            )

    def begin_turn(self, turn_id: str, kind: str, source_ids: list[str]) -> str:
        now = time.time()
        with self._db:
            row = self._db.execute(
                "SELECT state, external_effect_started FROM turns WHERE id=?",
                (turn_id,),
            ).fetchone()
            if row is None:
                self._db.execute(
                    """INSERT INTO turns
                       (id, kind, source_ids_json, state, started_at, updated_at)
                       VALUES (?, ?, ?, 'running', ?, ?)""",
                    (turn_id, kind, json.dumps(source_ids), now, now),
                )
                return "running"
            if row["state"] == "running" and row["external_effect_started"]:
                self._db.execute(
                    """UPDATE turns SET state='needs_reconciliation',
                       stage='needs_reconciliation',
                       failure_reason='process_interrupted_after_external_effect',
                       updated_at=? WHERE id=?""",
                    (now, turn_id),
                )
                self._open_reconciliation(
                    turn_id, "process_interrupted_after_external_effect", now
                )
                return "needs_reconciliation"
            if row["state"] == "running":
                self._db.execute(
                    """UPDATE turns SET stage='started', failure_reason=NULL,
                       llm_calls=0, input_tokens=0, output_tokens=0,
                       started_at=?, updated_at=? WHERE id=?""",
                    (now, now, turn_id),
                )
            return str(row["state"])

    def queue_memory_maintenance_turn(
        self, turn_id: str, source_id: str
    ) -> bool:
        now = time.time()
        with self._db:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO turns
                   (id, kind, source_ids_json, state, stage, started_at, updated_at)
                   VALUES (?, 'autonomous', ?, 'running',
                           'memory_maintenance_queued', ?, ?)""",
                (turn_id, json.dumps([source_id]), now, now),
            )
        return cursor.rowcount == 1

    def pending_memory_maintenance_turn(self) -> str | None:
        row = self._db.execute(
            """SELECT id FROM turns
               WHERE state='running'
                 AND stage IN (
                   'memory_maintenance_queued',
                   'memory_maintenance_running'
                 )
                 AND (
                   failure_reason IS NULL
                   OR updated_at<=?
                 )
               ORDER BY started_at, id LIMIT 1""",
            (time.time() - 300,),
        ).fetchone()
        return str(row["id"]) if row is not None else None

    def recover_memory_maintenance_turns(self) -> list[str]:
        with self._db:
            self._db.execute(
                """UPDATE turns SET stage='memory_maintenance_queued',
                   failure_reason=NULL, updated_at=?
                   WHERE state='running'
                     AND stage='memory_maintenance_running'""",
                (time.time(),),
            )
            rows = self._db.execute(
                """SELECT id FROM turns
                   WHERE state='running'
                     AND stage='memory_maintenance_queued'
                   ORDER BY started_at, id"""
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def claim_memory_maintenance_turn(self, turn_id: str) -> bool:
        with self._db:
            cursor = self._db.execute(
                """UPDATE turns SET stage='memory_maintenance_running',
                   failure_reason=NULL, updated_at=?
                   WHERE id=? AND state='running'
                     AND stage='memory_maintenance_queued'""",
                (time.time(), turn_id),
            )
        return cursor.rowcount == 1

    def release_memory_maintenance_turn(
        self, turn_id: str, reason: str
    ) -> None:
        with self._db:
            self._db.execute(
                """UPDATE turns SET stage='memory_maintenance_queued',
                   failure_reason=?, updated_at=?
                   WHERE id=? AND state='running'
                     AND stage='memory_maintenance_running'""",
                (reason[:500], time.time(), turn_id),
            )

    def memory_maintenance_source_ids(self, turn_id: str) -> list[str]:
        row = self._db.execute(
            "SELECT source_ids_json FROM turns WHERE id=?", (turn_id,)
        ).fetchone()
        if row is None:
            return []
        try:
            value = json.loads(str(row["source_ids_json"]))
        except (json.JSONDecodeError, TypeError):
            return []
        return [str(item) for item in value] if isinstance(value, list) else []

    def memory_maintenance_journal(
        self, turn_id: str
    ) -> list[dict[str, object]]:
        rows = self._db.execute(
            """SELECT item_type, payload_json FROM turn_journal
               WHERE turn_id=? AND item_type LIKE 'memory_maintenance_%'
               ORDER BY sequence""",
            (turn_id,),
        ).fetchall()
        items: list[dict[str, object]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict):
                items.append({"item_type": str(row["item_type"]), **payload})
        return items

    def latest_memory_maintenance_completion(
        self,
    ) -> dict[str, object] | None:
        rows = self._db.execute(
            """SELECT j.payload_json FROM turn_journal AS j
               JOIN turns AS t ON t.id=j.turn_id
               WHERE j.item_type='memory_maintenance_complete'
                 AND t.state='completed'
               ORDER BY j.created_at DESC, j.sequence DESC"""
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict):
                return payload
        return None

    def memory_maintenance_bootstrap_complete(self) -> bool:
        row = self._db.execute(
            """SELECT 1 FROM turn_journal AS j
               JOIN turns AS t ON t.id=j.turn_id
               WHERE j.item_type='memory_maintenance_complete'
                 AND t.state='completed'
                 AND json_extract(j.payload_json, '$.mode')='bootstrap'
               LIMIT 1"""
        ).fetchone()
        return row is not None

    def latest_owner_event_marker(
        self, *, through: float | None = None
    ) -> tuple[float, str]:
        if through is None:
            row = self._db.execute(
                """SELECT received_at, id FROM events
                   ORDER BY received_at DESC, id DESC LIMIT 1"""
            ).fetchone()
        else:
            row = self._db.execute(
                """SELECT received_at, id FROM events
                   WHERE received_at<=?
                   ORDER BY received_at DESC, id DESC LIMIT 1""",
                (through,),
            ).fetchone()
        return (
            (float(row["received_at"]), str(row["id"]))
            if row is not None
            else (0.0, "")
        )

    def memory_maintenance_owner_evidence(
        self,
        *,
        after_at: float,
        after_id: str,
        through_at: float,
        through_id: str,
    ) -> list[dict[str, object]]:
        rows = self._db.execute(
            """SELECT id, content, occurred_at, received_at FROM events
               WHERE (received_at>? OR (received_at=? AND id>?))
                 AND (received_at<? OR (received_at=? AND id<=?))
               ORDER BY received_at, id""",
            (
                after_at,
                after_at,
                after_id,
                through_at,
                through_at,
                through_id,
            ),
        ).fetchall()
        return [
            {
                "event_id": str(row["id"]),
                "content": str(row["content"]),
                "occurred_at": context_timestamp(row["occurred_at"]),
                "received_at": float(row["received_at"]),
            }
            for row in rows
        ]

    def memory_maintenance_evidence_for_memories(
        self, memory_ids: list[int]
    ) -> list[dict[str, object]]:
        if not memory_ids:
            return []
        placeholders = ",".join("?" for _ in memory_ids)
        rows = self._db.execute(
            f"""SELECT DISTINCT v.id, v.content, v.occurred_at, v.received_at
                FROM memory_evidence AS e
                JOIN events AS v ON v.id=e.source_event_id
                WHERE e.memory_id IN ({placeholders})
                ORDER BY v.received_at, v.id""",
            memory_ids,
        ).fetchall()
        return [
            {
                "event_id": str(row["id"]),
                "content": str(row["content"]),
                "occurred_at": context_timestamp(row["occurred_at"]),
                "received_at": float(row["received_at"]),
            }
            for row in rows
        ]

    def memory_maintenance_changed_ids(
        self, *, after: float, through: float
    ) -> set[int]:
        rows = self._db.execute(
            """SELECT id FROM memories AS m
               WHERE m.updated_at>? AND m.updated_at<=?
                 AND m.superseded_by IS NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )""",
            (after, through),
        ).fetchall()
        return {int(row["id"]) for row in rows}

    def apply_memory_maintenance_batch(
        self,
        turn_id: str,
        decision: dict[str, object],
        mutable_memories: dict[int, dict[str, object]],
        *,
        owner_marker: tuple[float, str],
    ) -> None:
        now = time.time()
        with self._db:
            if self.latest_owner_event_marker() != owner_marker:
                raise ValueError("owner_evidence_changed")
            current: dict[int, dict[str, object]] = {}
            for memory_id, snapshot in mutable_memories.items():
                row = self._db.execute(
                    """SELECT id, kind, key, content, activation, authority,
                              source_event_id, evidence_quote, importance,
                              created_at, updated_at, expires_at, superseded_by
                       FROM memories AS m
                       WHERE m.id=? AND m.superseded_by IS NULL
                         AND (m.expires_at IS NULL OR m.expires_at>?)
                         AND NOT EXISTS (
                           SELECT 1 FROM memory_tombstones AS t
                           WHERE t.kind=m.kind AND t.key=m.key
                         )""",
                    (memory_id, now),
                ).fetchone()
                if row is None:
                    raise ValueError("memory_snapshot_changed")
                item = dict(row)
                if memory_snapshot_fingerprint(item) != memory_snapshot_fingerprint(
                    snapshot
                ):
                    raise ValueError("memory_snapshot_changed")
                current[memory_id] = item

            for change in decision.get("changes", []):
                if not isinstance(change, dict):
                    raise ValueError("invalid_memory_maintenance_change")
                action = str(change["action"])
                if action == "replace":
                    memory_id = int(change["memory_id"])
                    row = current[memory_id]
                    activation = str(change["activation"])
                    expires_at = change.get("expires_at")
                    if row["activation"] != "always" and activation == "always":
                        raise ValueError("memory_maintenance_promotes_always")
                    if activation == "recent":
                        if (
                            isinstance(expires_at, bool)
                            or not isinstance(expires_at, (int, float))
                            or not now < float(expires_at) <= now + 7 * 86400
                        ):
                            raise ValueError("invalid_memory_maintenance_expiry")
                    elif expires_at is not None:
                        raise ValueError("invalid_memory_maintenance_expiry")
                    evidence = change.get("evidence")
                    if isinstance(evidence, dict):
                        event_id = str(evidence["event_id"])
                        quote = str(evidence["quote"])
                        event = self._db.execute(
                            "SELECT content, occurred_at FROM events WHERE id=?",
                            (event_id,),
                        ).fetchone()
                        if event is None or quote not in str(event["content"]):
                            raise ValueError("invalid_memory_maintenance_evidence")
                        source_event_id = event_id
                        evidence_quote = quote
                        updated_at = float(event["occurred_at"])
                    else:
                        source_event_id = str(row["source_event_id"])
                        evidence_quote = str(row["evidence_quote"])
                        updated_at = float(row["updated_at"])
                    self._db.execute(
                        """UPDATE memories SET content=?, activation=?,
                           expires_at=?, source_event_id=?, evidence_quote=?,
                           updated_at=? WHERE id=? AND superseded_by IS NULL""",
                        (
                            str(change["content"]),
                            activation,
                            expires_at,
                            source_event_id,
                            evidence_quote,
                            updated_at,
                            memory_id,
                        ),
                    )
                    if isinstance(evidence, dict):
                        self._add_memory_evidence(
                            memory_id, source_event_id, evidence_quote, updated_at
                        )
                elif action == "merge":
                    survivor_id = int(change["survivor_id"])
                    source_ids = [int(item) for item in change["source_ids"]]
                    survivor = current[survivor_id]
                    activation = str(change["activation"])
                    expires_at = change.get("expires_at")
                    if survivor["activation"] != "always" and activation == "always":
                        raise ValueError("memory_maintenance_promotes_always")
                    if activation == "recent":
                        if (
                            isinstance(expires_at, bool)
                            or not isinstance(expires_at, (int, float))
                            or not now < float(expires_at) <= now + 7 * 86400
                        ):
                            raise ValueError("invalid_memory_maintenance_expiry")
                    elif expires_at is not None:
                        raise ValueError("invalid_memory_maintenance_expiry")
                    evidence_event_ids = [
                        str(event_id) for event_id in change["evidence_event_ids"]
                    ]
                    placeholders = ",".join("?" for _ in evidence_event_ids)
                    cited_events = self._db.execute(
                        f"""SELECT id,content,occurred_at FROM events
                            WHERE id IN ({placeholders})""",
                        evidence_event_ids,
                    ).fetchall()
                    if len(cited_events) != len(evidence_event_ids):
                        raise ValueError("invalid_memory_maintenance_evidence")
                    newest_event = max(
                        cited_events, key=lambda item: float(item["occurred_at"])
                    )
                    for source_id in source_ids:
                        self._db.execute(
                            """INSERT OR IGNORE INTO memory_evidence
                               (memory_id, source_event_id, quote, created_at)
                               SELECT ?, source_event_id, quote, created_at
                               FROM memory_evidence WHERE memory_id=?""",
                            (survivor_id, source_id),
                        )
                        self._db.execute(
                            """UPDATE memories SET superseded_by=?
                               WHERE id=? AND superseded_by IS NULL""",
                            (survivor_id, source_id),
                        )
                    for event in cited_events:
                        self._add_memory_evidence(
                            survivor_id,
                            str(event["id"]),
                            str(event["content"]),
                            float(event["occurred_at"]),
                        )
                    self._db.execute(
                        """UPDATE memories SET content=?, activation=?, expires_at=?,
                           source_event_id=?, evidence_quote=?, updated_at=?
                           WHERE id=? AND superseded_by IS NULL""",
                        (
                            str(change["content"]),
                            activation,
                            expires_at,
                            newest_event["id"],
                            newest_event["content"],
                            newest_event["occurred_at"],
                            survivor_id,
                        ),
                    )
                elif action == "retire":
                    memory_id = int(change["memory_id"])
                    row = current[memory_id]
                    evidence = change["evidence"]
                    assert isinstance(evidence, dict)
                    event_id = str(evidence["event_id"])
                    quote = str(evidence["quote"])
                    event = self._db.execute(
                        "SELECT content FROM events WHERE id=?", (event_id,)
                    ).fetchone()
                    if event is None or quote not in str(event["content"]):
                        raise ValueError("invalid_memory_maintenance_evidence")
                    sibling = self._db.execute(
                        """SELECT 1 FROM memories
                           WHERE kind=? AND key=? AND id<>?
                             AND superseded_by IS NULL LIMIT 1""",
                        (row["kind"], row["key"], memory_id),
                    ).fetchone()
                    if sibling is not None:
                        raise ValueError("memory_maintenance_tombstone_conflict")
                    self._db.execute(
                        """INSERT INTO memory_tombstones
                           (kind, key, source_event_id, evidence_quote, created_at)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(kind,key) DO UPDATE SET
                             source_event_id=excluded.source_event_id,
                             evidence_quote=excluded.evidence_quote,
                             created_at=excluded.created_at""",
                        (row["kind"], row["key"], event_id, quote, now),
                    )
                else:
                    raise ValueError("invalid_memory_maintenance_change")

            self._append_turn_journal(
                turn_id,
                "memory_maintenance_batch",
                {
                    "reviewed_ids": list(decision.get("reviewed_ids", [])),
                    "completed_ids": list(decision.get("completed_ids", [])),
                    "regroup_requests": list(
                        decision.get("regroup_requests", [])
                    ),
                    "summary": str(decision.get("summary") or ""),
                    "owner_marker": [owner_marker[0], owner_marker[1]],
                },
                visibility="internal",
                trust="runtime",
                created_at=now,
            )

    def cancel_turn(
        self, turn_id: str, events: list[IncomingMessage] | None = None
    ) -> None:
        with self._db:
            row = self._db.execute(
                "SELECT external_effect_started FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            self._db.execute(
                """UPDATE turns SET state='cancelled', stage='cancelled',
                   failure_reason='owner_stop', updated_at=? WHERE id=?""",
                (time.time(), turn_id),
            )
            if row is not None and row["external_effect_started"]:
                self._open_reconciliation(
                    turn_id, "owner_stopped_after_external_effect", time.time()
                )
            self._db.executemany(
                "UPDATE events SET processed=1 WHERE id=?",
                ((event.event_id,) for event in events or []),
            )

    def record_turn_failure(self, turn_id: str, reason: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE turns SET failure_reason=?, updated_at=? WHERE id=?",
                (reason[:500], time.time(), turn_id),
            )

    def complete_background_turn(self, turn_id: str) -> None:
        with self._db:
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   failure_reason=NULL, updated_at=? WHERE id=?""",
                (time.time(), turn_id),
            )

    def turn_has_external_effect(self, turn_id: str) -> bool:
        row = self._db.execute(
            "SELECT external_effect_started FROM turns WHERE id=?", (turn_id,)
        ).fetchone()
        return bool(row and row["external_effect_started"])

    def turn_usage(self, turn_id: str) -> dict[str, float | int]:
        row = self._db.execute(
            """SELECT started_at, llm_calls, input_tokens, output_tokens
               FROM turns WHERE id=?""",
            (turn_id,),
        ).fetchone()
        if row is None:
            return {"started_at": time.time(), "llm_calls": 0, "input": 0, "output": 0}
        return {
            "started_at": float(row["started_at"]),
            "llm_calls": int(row["llm_calls"]),
            "input": int(row["input_tokens"]),
            "output": int(row["output_tokens"]),
        }

    def record_turn_usage(
        self, turn_id: str, input_tokens: int, output_tokens: int
    ) -> None:
        with self._db:
            self._db.execute(
                """UPDATE turns SET llm_calls=llm_calls+1,
                   input_tokens=input_tokens+?, output_tokens=output_tokens+?,
                   updated_at=? WHERE id=?""",
                (
                    max(0, input_tokens),
                    max(0, output_tokens),
                    time.time(),
                    turn_id,
                ),
            )

    def record_llm_call(
        self,
        *,
        created_at: float,
        turn_id: str = "",
        stage: str = "",
        model: str = "",
        metrics: dict[str, float | int | bool],
    ) -> None:
        with self._db:
            self._db.execute(
                """INSERT INTO llm_usage
                   (created_at, turn_id, stage, model, input_tokens,
                    uncached_tokens, cache_read_tokens, cache_write_tokens,
                    output_tokens, cache_reported)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    created_at,
                    turn_id,
                    stage,
                    model,
                    max(0, int(metrics.get("input") or 0)),
                    max(0, int(metrics.get("uncached") or 0)),
                    max(0, int(metrics.get("cache_read") or 0)),
                    max(0, int(metrics.get("cache_write") or 0)),
                    max(0, int(metrics.get("output") or 0)),
                    1 if metrics.get("cache_reported") else 0,
                ),
            )

    def record_thinking_call(
        self,
        *,
        created_at: float,
        turn_id: str = "",
        call_id: str = "",
        stage: str = "",
        round: int = 0,
        model: str = "",
        tools: list[str] | None = None,
        reasoning: str = "",
    ) -> None:
        self._thinking.record(
            created_at=created_at,
            turn_id=turn_id,
            call_id=call_id,
            stage=stage,
            round=round,
            model=model,
            tools=list(tools or []),
            reasoning=reasoning,
        )

    def search_thinking(
        self,
        *,
        turn_id: str = "",
        query: str = "",
        after: float | None = None,
        before: float | None = None,
        stage: str = "",
        limit: int = 5,
        cursor: int = 0,
    ) -> dict[str, object]:
        hint_at = None
        if turn_id and after is None and before is None:
            row = self._db.execute(
                "SELECT started_at FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            if row is not None:
                hint_at = float(row["started_at"])
        return self._thinking.search(
            turn_id=turn_id,
            query=query,
            after=after,
            before=before,
            stage=stage,
            limit=limit,
            cursor=cursor,
            hint_at=hint_at,
        )

    def read_thinking(self, turn_id: str, call_id: str = "") -> dict[str, object]:
        return self._thinking.read(turn_id, call_id)

    def dashboard_thinking(
        self,
        *,
        month: str = "",
        limit: int = 64,
        cursor: int = 0,
    ) -> dict[str, object]:
        months = self._thinking.available_months()
        selected = str(month or "").strip()
        after: float | None = None
        before: float | None = None
        if selected == "all":
            pass
        else:
            if not selected:
                selected = datetime.now().astimezone().strftime("%Y-%m")
            selected = parse_month(selected)
            after, before = month_bounds(selected)
            if selected not in months:
                months = sorted({*months, selected})
        found = self.search_thinking(
            after=after,
            before=before,
            limit=5000,
            cursor=0,
        )
        turns = _group_thinking_turns(found.get("calls") or [])
        start = max(0, cursor)
        size = min(200, max(1, limit))
        page = turns[start : start + size]
        result: dict[str, object] = {
            "ok": True,
            "month": selected,
            "months": months,
            "items": page,
            "count": len(page),
        }
        next_cursor = start + size
        if next_cursor < len(turns):
            result["next_cursor"] = next_cursor
        linked = self.episodes_for_turns(
            [str(item.get("turn_id") or "") for item in page]
        )
        for item in page:
            episode = linked.get(str(item.get("turn_id") or ""))
            if episode:
                item["episode_id"] = episode["episode_id"]
                item["episode_title"] = episode["episode_title"]
        return result

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

    def dashboard_usage(
        self, *, days: int = 30, now: float | None = None
    ) -> dict[str, object]:
        current = time.time() if now is None else now
        zone = datetime.now().astimezone().tzinfo or ZoneInfo("UTC")
        start = datetime.fromtimestamp(current, zone).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=max(1, days) - 1)
        rows = self._db.execute(
            """SELECT created_at, turn_id, stage, model, input_tokens,
                      uncached_tokens, cache_read_tokens, cache_write_tokens,
                      output_tokens, cache_reported
               FROM llm_usage
               WHERE created_at >= ?
               ORDER BY created_at""",
            (start.timestamp(),),
        ).fetchall()
        plugin = self._usage_plugin
        return summarize_usage(
            [dict(row) for row in rows],
            days=days,
            now=current,
            zone=zone,
            estimate=None if plugin is None else plugin.estimate_cost,
            note=PRICING_NOTE,
        )

    @staticmethod
    def _context_plan_dict(row: sqlite3.Row) -> dict[str, object]:
        plan = dict(row)
        plan["source_event_ids"] = json.loads(str(plan.pop("source_event_ids_json")))
        plan["plan"] = json.loads(str(plan.pop("plan_json")))
        plan["retrieval"] = json.loads(str(plan.pop("retrieval_json")))
        return plan

    def save_context_plan(
        self,
        turn_id: str,
        revision: int,
        source_event_ids: list[str],
        plan: dict[str, object],
        *,
        state: str = "planned",
    ) -> dict[str, object]:
        if revision < 1:
            raise ValueError("context plan revision must be positive")
        if state not in {"planned", "degraded"}:
            raise ValueError("a new context plan must be planned or degraded")
        source_json = json.dumps(
            source_event_ids, ensure_ascii=False, separators=(",", ":")
        )
        plan_json = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
        now = time.time()
        with self._db:
            existing = self._db.execute(
                "SELECT * FROM context_plans WHERE turn_id=? AND revision=?",
                (turn_id, revision),
            ).fetchone()
            if existing is not None:
                if (
                    json.loads(str(existing["source_event_ids_json"]))
                    != source_event_ids
                    or json.loads(str(existing["plan_json"])) != plan
                ):
                    raise ValueError("context plan revision already exists")
                return self._context_plan_dict(existing)
            latest = self._db.execute(
                "SELECT MAX(revision) FROM context_plans WHERE turn_id=?", (turn_id,)
            ).fetchone()[0]
            if latest is not None and int(latest) >= revision:
                raise ValueError("context plan revision must increase")
            self._db.execute(
                """UPDATE context_plans SET state='superseded', updated_at=?
                   WHERE turn_id=? AND state<>'superseded'""",
                (now, turn_id),
            )
            self._db.execute(
                """INSERT INTO context_plans
                   (turn_id, revision, source_event_ids_json, plan_json,
                    state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (turn_id, revision, source_json, plan_json, state, now, now),
            )
        saved = self.context_plan(turn_id, revision)
        if saved is None:
            raise RuntimeError("context plan was not saved")
        return saved

    def context_plan(
        self, turn_id: str, revision: int | None = None
    ) -> dict[str, object] | None:
        if revision is None:
            row = self._db.execute(
                """SELECT * FROM context_plans
                   WHERE turn_id=? AND state<>'superseded'
                   ORDER BY revision DESC LIMIT 1""",
                (turn_id,),
            ).fetchone()
        else:
            row = self._db.execute(
                "SELECT * FROM context_plans WHERE turn_id=? AND revision=?",
                (turn_id, revision),
            ).fetchone()
        return self._context_plan_dict(row) if row else None

    def recall_reuse_candidates(self, turn_ids: list[str]) -> list[dict[str, object]]:
        """Return only the latest recalled Turn and its effective search scope."""

        ordered_ids = [turn_id for turn_id in dict.fromkeys(turn_ids) if turn_id]
        if not ordered_ids:
            return []
        query_cache: dict[str, list[str]] = {}
        resolving: set[str] = set()

        def effective_queries(turn_id: str) -> list[str]:
            cached = query_cache.get(turn_id)
            if cached is not None:
                return cached
            if turn_id in resolving:
                return []
            resolving.add(turn_id)
            record = self.context_plan(turn_id)
            if record is None or record.get("state") != "recalled":
                queries: list[str] = []
            else:
                retrieval = record.get("retrieval")
                plan = record.get("plan")
                if (
                    not isinstance(retrieval, dict)
                    or retrieval.get("version") not in {4, 5, 6}
                    or not isinstance(plan, dict)
                ):
                    queries = []
                else:
                    query_recall = str(retrieval.get("query_recall") or "")
                    stored = retrieval.get("effective_recall_queries")
                    if isinstance(stored, list):
                        queries = [
                            text[:240]
                            for query in stored
                            for text in _recall_query_texts(query)[:1]
                        ]
                    elif "misses=" in query_recall:
                        queries = []
                    else:
                        units = [
                            unit
                            for unit in plan.get("intent_units") or []
                            if isinstance(unit, dict)
                        ]
                        queries = [
                            text[:240]
                            for unit in units
                            for query in unit.get("recall_queries") or []
                            for text in _recall_query_texts(query)[:1]
                        ]
                        for source_turn_id in dict.fromkeys(
                            str(unit.get("recall_from_turn_id") or "")
                            for unit in units
                            if unit.get("recall_from_turn_id")
                        ):
                            queries.extend(effective_queries(source_turn_id))
                    queries = list(dict.fromkeys(queries))
            resolving.discard(turn_id)
            query_cache[turn_id] = queries
            return queries

        for turn_id in reversed(ordered_ids):
            queries = effective_queries(turn_id)
            if not queries:
                continue
            return [{"turn_id": turn_id, "queries": queries}]
        return []

    def save_context_retrieval(
        self,
        turn_id: str,
        revision: int,
        retrieval: dict[str, object],
        *,
        state: str = "recalled",
    ) -> dict[str, object]:
        if state not in {"recalled", "degraded"}:
            raise ValueError("context retrieval must be recalled or degraded")
        now = time.time()
        with self._db:
            cursor = self._db.execute(
                """UPDATE context_plans SET retrieval_json=?, state=?, updated_at=?
                   WHERE turn_id=? AND revision=? AND state<>'superseded'""",
                (
                    json.dumps(retrieval, ensure_ascii=False, separators=(",", ":")),
                    state,
                    now,
                    turn_id,
                    revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("active context plan not found")
        saved = self.context_plan(turn_id, revision)
        if saved is None:
            raise RuntimeError("context retrieval was not saved")
        return saved

    @staticmethod
    def _episode_dict(row: sqlite3.Row) -> dict[str, object]:
        episode = dict(row)
        episode.pop("overlap", None)
        _add_context_timestamps(
            episode,
            ("created_at", "updated_at", "closed_at", "summary_abandoned_at"),
        )
        try:
            claims = json.loads(str(episode.pop("working_summary_claims_json")))
        except (json.JSONDecodeError, TypeError):
            claims = []
        episode["working_summary_claims"] = claims if isinstance(claims, list) else []
        try:
            emotional_context = json.loads(str(episode.pop("emotional_context_json")))
        except (json.JSONDecodeError, TypeError):
            emotional_context = {}
        try:
            outcomes = json.loads(str(episode.pop("outcomes_json")))
        except (json.JSONDecodeError, TypeError):
            outcomes = []
        episode["emotional_context"] = (
            emotional_context if isinstance(emotional_context, dict) else {}
        )
        episode["outcomes"] = outcomes if isinstance(outcomes, list) else []
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
        plan = json.loads(str(plan_row["plan_json"])) if plan_row else {}
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
                            for text in _recall_query_texts(query)
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

    def _ensure_autonomous_episode(
        self,
        episode_key: str,
        turn_id: str,
        title: str,
        now: float,
        *recall_values: object,
    ) -> str:
        episode_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"momoi:autonomous-episode:{episode_key}"
        ).hex
        self._db.execute(
            """INSERT OR IGNORE INTO turns
               (id, kind, source_ids_json, state, started_at, updated_at)
               VALUES (?, 'autonomous', ?, 'running', ?, ?)""",
            (turn_id, json.dumps([episode_key]), now, now),
        )
        self._db.execute(
            """INSERT OR IGNORE INTO conversation_episodes
               (id, title, salience, created_at, updated_at)
               VALUES (?, ?, 0.4, ?, ?)""",
            (episode_id, title[:200], now, now),
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
                   (id, title, salience, created_at, updated_at)
                   VALUES (?, ?, 0.4, ?, ?)""",
                (episode_id, title[:200], now, now),
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
            "UPDATE conversation_episodes SET updated_at=? WHERE id=?",
            (now, episode_id),
        )
        self._index_episode_terms(episode_id, title, *recall_values)
        self._index_turn_episode_terms(turn_id)
        return episode_id

    def _runtime_archive_kind(self, episode_id: str) -> str | None:
        """Return the runtime owner of a Webhook or Heartbeat day archive."""
        row = self._db.execute(
            """SELECT CASE
                     WHEN EXISTS (
                         SELECT 1 FROM episode_turns AS archive_turn
                         WHERE archive_turn.episode_id=?
                           AND archive_turn.turn_id GLOB 'webhook:*'
                     ) THEN 'webhook'
                     WHEN EXISTS (
                         SELECT 1 FROM episode_turns AS archive_turn
                         JOIN turns AS archive_source
                           ON archive_source.id=archive_turn.turn_id
                         WHERE archive_turn.episode_id=?
                           AND archive_source.kind='autonomous'
                           AND EXISTS (
                               SELECT 1
                               FROM json_each(archive_source.source_ids_json)
                                    AS source_id
                               WHERE source_id.value GLOB 'heartbeat:*'
                           )
                     ) THEN 'heartbeat'
                   END AS kind""",
            (episode_id, episode_id),
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
            item["timestamp"] = context_timestamp(item["created_at"])
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
        turns = self._db.execute(
            """SELECT t.* FROM turns AS t
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
                        "timestamp": context_timestamp(message["created_at"]),
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
                try:
                    payload = json.loads(str(item["payload_json"]))
                except (TypeError, json.JSONDecodeError):
                    payload = {"error": "invalid_journal_payload"}
                if not isinstance(payload, dict):
                    payload = {"value": payload}
                if item["item_type"] == "final":
                    final.update(payload)
                    continue
                timeline.append(
                    {
                        "type": str(item["item_type"]),
                        "timestamp": context_timestamp(item["created_at"]),
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
                try:
                    plan = json.loads(str(plan_row["plan_json"]))
                except (TypeError, json.JSONDecodeError):
                    plan = {}
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
                    "state": str(turn["state"]),
                    "channel": str(final.get("channel") or ""),
                    "started_at": context_timestamp(turn["started_at"]),
                    "completed_at": context_timestamp(turn["updated_at"]),
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
                try:
                    source_ids = json.loads(str(row["source_ids_json"] or "[]"))
                except (TypeError, json.JSONDecodeError):
                    source_ids = []
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
            """SELECT e.*, COALESCE((
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
               AND (?=0 OR NOT EXISTS (
                   SELECT 1 FROM episode_turns AS archive_turn
                   WHERE archive_turn.episode_id=e.id
                     AND (
                         archive_turn.turn_id GLOB 'webhook:*'
                         OR EXISTS (
                             SELECT 1 FROM turns AS archive_source
                             WHERE archive_source.id=archive_turn.turn_id
                               AND archive_source.kind='autonomous'
                               AND EXISTS (
                                   SELECT 1
                                   FROM json_each(
                                       archive_source.source_ids_json
                                   ) AS source_id
                                   WHERE source_id.value GLOB 'heartbeat:*'
                               )
                         )
                     )
               ))
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
            episode["last_activity_timestamp"] = context_timestamp(
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
            """SELECT e.*, COALESCE((
                       SELECT MAX(t.updated_at) FROM episode_turns AS et
                       JOIN turns AS t ON t.id=et.turn_id
                       WHERE et.episode_id=e.id
                   ), e.updated_at) AS last_activity_at
               FROM conversation_episodes AS e
               WHERE ?=0 OR NOT EXISTS (
                   SELECT 1 FROM episode_turns AS archive_turn
                   WHERE archive_turn.episode_id=e.id
                     AND (
                         archive_turn.turn_id GLOB 'webhook:*'
                         OR EXISTS (
                             SELECT 1 FROM turns AS archive_source
                             WHERE archive_source.id=archive_turn.turn_id
                               AND archive_source.kind='autonomous'
                               AND EXISTS (
                                   SELECT 1
                                   FROM json_each(
                                       archive_source.source_ids_json
                                   ) AS source_id
                                   WHERE source_id.value GLOB 'heartbeat:*'
                               )
                         )
                     )
               )
               ORDER BY last_activity_at DESC, e.id DESC
               LIMIT ?""",
            (int(exclude_runtime_archives), limit),
        ).fetchall()
        results = []
        for row in rows:
            episode = self._episode_dict(row)
            episode["last_activity_timestamp"] = context_timestamp(
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
                    "last_activity_timestamp": context_timestamp(
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
                    "id": f"turn:{row['id']}",
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
            "id": f"turn:{turn_id}",
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
            """SELECT e.id, e.status, e.title, e.working_summary, e.open_loops_json,
                      e.updated_at,
                      COALESCE((
                          SELECT MAX(t.updated_at) FROM episode_turns AS et
                          JOIN turns AS t ON t.id=et.turn_id
                          WHERE et.episode_id=e.id
                      ), e.updated_at) AS last_activity_at
               FROM conversation_episodes AS e
               WHERE e.status IN ('open', 'closing')
                 AND NOT EXISTS (
                     SELECT 1 FROM episode_turns AS archive_turn
                     WHERE archive_turn.episode_id=e.id
                       AND (
                           archive_turn.turn_id GLOB 'webhook:*'
                           OR EXISTS (
                               SELECT 1 FROM turns AS archive_source
                               WHERE archive_source.id=archive_turn.turn_id
                                 AND archive_source.kind='autonomous'
                                 AND EXISTS (
                                     SELECT 1
                                     FROM json_each(
                                         archive_source.source_ids_json
                                     ) AS source_id
                                     WHERE source_id.value GLOB 'heartbeat:*'
                                 )
                           )
                       )
                 )
               ORDER BY e.status='open' DESC, last_activity_at DESC, e.updated_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        inventory: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            try:
                open_loops = json.loads(str(item.pop("open_loops_json")))
            except (json.JSONDecodeError, TypeError):
                open_loops = []
            item["open_loops"] = open_loops if isinstance(open_loops, list) else []
            item["last_activity_timestamp"] = context_timestamp(
                item["last_activity_at"]
            )
            item["updated_timestamp"] = context_timestamp(item.pop("updated_at"))
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
            str(row["turn_id"]): json.loads(str(row["plan_json"]))
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
                        for text in _recall_query_texts(item)
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
                    timestamp=context_timestamp(message["created_at"]),
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
            episode["last_activity_timestamp"] = context_timestamp(
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
            episode["last_activity_timestamp"] = context_timestamp(
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
            "timestamp": context_timestamp(row["created_at"]),
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
            source_kind = "webhook" if turn_id.startswith("webhook:") else None
            if source_kind is None:
                source = self._db.execute(
                    """SELECT 1 FROM turns AS archive_source
                       WHERE archive_source.id=?
                         AND archive_source.kind='autonomous'
                         AND EXISTS (
                             SELECT 1
                             FROM json_each(archive_source.source_ids_json)
                                  AS source_id
                             WHERE source_id.value GLOB 'heartbeat:*'
                         )""",
                    (turn_id,),
                ).fetchone()
                source_kind = "heartbeat" if source is not None else None
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
            item["timestamp"] = context_timestamp(item["created_at"])
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

    def _consolidation_turn_messages(
        self, turn_ids: list[str]
    ) -> dict[str, list[dict[str, object]]]:
        by_turn: dict[str, list[dict[str, object]]] = {
            turn_id: [] for turn_id in turn_ids
        }
        if not turn_ids:
            return by_turn
        placeholders = ",".join("?" for _ in turn_ids)
        messages = self._db.execute(
            f"""SELECT id, turn_id, role, content, created_at, delivery_state
                FROM messages
                WHERE turn_id IN ({placeholders})
                  AND (role IN ('user', 'event') OR delivery_state IN
                       ('delivered', 'uncertain', 'internal'))
                ORDER BY id""",
            tuple(turn_ids),
        ).fetchall()
        for row in messages:
            item = dict(row)
            item["timestamp"] = context_timestamp(item["created_at"])
            by_turn[str(row["turn_id"])].append(item)
        return by_turn

    def _upsert_consolidation_decision(
        self,
        turn_id: str,
        action: str,
        reason: str,
        now: float,
        episode_id: str | None = None,
    ) -> None:
        self._db.execute(
            """INSERT INTO episode_consolidation_decisions
               (turn_id, action, episode_id, reason, processed_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(turn_id) DO UPDATE SET
                 action=excluded.action,
                 episode_id=excluded.episode_id,
                 reason=excluded.reason,
                 processed_at=excluded.processed_at""",
            (turn_id, action, episode_id, reason[:500], now),
        )

    def _episode_consolidation_pending_rows(
        self, limit: int
    ) -> list[sqlite3.Row]:
        limit = max(1, limit)
        return self._db.execute(
            """SELECT pending.id, pending.updated_at FROM (
                   SELECT t.id, t.updated_at FROM turns AS t
                   WHERE t.kind='owner' AND t.state='completed'
                     AND NOT EXISTS (
                         SELECT 1 FROM episode_turns AS et WHERE et.turn_id=t.id
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM episode_consolidation_decisions AS d
                         WHERE d.turn_id=t.id AND d.action IN ('ignored', 'linked')
                     )
                     AND (
                         NOT EXISTS (
                             SELECT 1 FROM episode_consolidation_decisions AS d
                             WHERE d.turn_id=t.id
                         )
                         OR EXISTS (
                             SELECT 1 FROM episode_consolidation_decisions AS d
                             WHERE d.turn_id=t.id AND d.action='deferred'
                               AND EXISTS (
                                   SELECT 1 FROM turns AS later
                                   WHERE later.kind='owner'
                                     AND later.state='completed'
                                     AND later.id<>t.id
                                     AND later.updated_at>d.processed_at
                               )
                         )
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM messages AS m
                         WHERE m.turn_id=t.id AND m.delivery_state='queued'
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM self_state AS state
                         WHERE state.id=1
                           AND state.pending_reply_turn_id=t.id
                           AND state.pending_reply_expectation<>''
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM outbox AS o
                         WHERE o.turn_id=t.id AND o.reply_expectation<>''
                           AND o.state IN ('pending', 'sending', 'ambiguous')
                     )
                     AND EXISTS (
                         SELECT 1 FROM messages AS m WHERE m.turn_id=t.id
                     )
                   ORDER BY t.updated_at DESC LIMIT ?
               ) AS pending
               ORDER BY pending.updated_at""",
            (limit,),
        ).fetchall()

    def episode_consolidation_pending_count(self, limit: int = 6) -> int:
        return len(self._episode_consolidation_pending_rows(limit))

    def claim_episode_consolidation_candidate(
        self, limit: int = 6, *, minimum: int = 6
    ) -> dict[str, object] | None:
        limit = max(1, limit)
        minimum = max(1, min(minimum, limit))
        rows = self._episode_consolidation_pending_rows(limit)
        if len(rows) < minimum:
            return None
        turn_ids = [str(row["id"]) for row in rows]
        by_turn = self._consolidation_turn_messages(turn_ids)
        oldest_updated = float(rows[0]["updated_at"])
        context_rows = self._db.execute(
            """SELECT t.id, t.updated_at, et.episode_id
               FROM turns AS t
               JOIN episode_turns AS et ON et.turn_id=t.id
               WHERE t.kind='owner' AND t.state='completed'
                 AND t.updated_at>?
                 AND NOT EXISTS (
                     SELECT 1 FROM episode_turns AS archive_turn
                     WHERE archive_turn.episode_id=et.episode_id
                       AND (
                           archive_turn.turn_id GLOB 'webhook:*'
                           OR EXISTS (
                               SELECT 1 FROM turns AS archive_source
                               WHERE archive_source.id=archive_turn.turn_id
                                 AND archive_source.kind='autonomous'
                                 AND EXISTS (
                                     SELECT 1
                                     FROM json_each(
                                         archive_source.source_ids_json
                                     ) AS source_id
                                     WHERE source_id.value GLOB 'heartbeat:*'
                                 )
                           )
                       )
                 )
               ORDER BY t.updated_at
               LIMIT 12""",
            (oldest_updated,),
        ).fetchall()
        context_ids = [str(row["id"]) for row in context_rows]
        context_messages = self._consolidation_turn_messages(context_ids)
        context_turns: list[dict[str, object]] = []
        extra_episodes: list[dict[str, object]] = []
        seen_episodes: set[str] = set()
        for row in context_rows:
            episode_id = str(row["episode_id"])
            episode = self.episode(episode_id)
            context_turns.append(
                {
                    "turn_id": str(row["id"]),
                    "timestamp": context_timestamp(row["updated_at"]),
                    "episode_id": episode_id,
                    "episode_title": "" if episode is None else episode["title"],
                    "messages": context_messages[str(row["id"])],
                }
            )
            if episode is not None and episode_id not in seen_episodes:
                extra_episodes.append(
                    {
                        "id": episode["id"],
                        "title": episode["title"],
                        "status": episode["status"],
                        "narrative_summary": episode["narrative_summary"],
                        "topics": episode["topics"],
                        "entities": episode["entities"],
                        "open_loops": episode["open_loops"],
                    }
                )
                seen_episodes.add(episode_id)
        candidate_episodes = [
            {
                "id": episode["id"],
                "title": episode["title"],
                "status": episode["status"],
                "narrative_summary": episode["narrative_summary"],
                "topics": episode["topics"],
                "entities": episode["entities"],
                "open_loops": episode["open_loops"],
            }
            for episode in self.list_episode_directory(
                12,
                after=time.time() - EPISODE_CONSOLIDATION_LOOKBACK_SECONDS,
                exclude_runtime_archives=True,
            )
        ]
        for episode in extra_episodes:
            if episode["id"] not in {item["id"] for item in candidate_episodes}:
                candidate_episodes.append(episode)
        return {
            "turns": [
                {
                    "turn_id": turn_id,
                    "timestamp": context_timestamp(row["updated_at"]),
                    "messages": by_turn[turn_id],
                }
                for turn_id, row in zip(turn_ids, rows, strict=True)
            ],
            "context_turns": context_turns,
            "candidate_episodes": candidate_episodes,
        }

    def episode_consolidation_remaining(
        self, turn_ids: list[str]
    ) -> list[str]:
        """Return fixed-batch Turns without a durable consolidation decision."""

        remaining: list[str] = []
        for turn_id in turn_ids:
            covered = self._db.execute(
                """SELECT EXISTS (
                       SELECT 1 FROM episode_turns WHERE turn_id=?
                   ) OR EXISTS (
                       SELECT 1 FROM episode_consolidation_decisions
                       WHERE turn_id=? AND action IN ('ignored', 'deferred', 'linked')
                   )""",
                (turn_id, turn_id),
            ).fetchone()[0]
            if not covered:
                remaining.append(turn_id)
        return remaining

    def apply_episode_consolidation(
        self,
        turn_ids: list[str],
        decisions: list[dict[str, object]],
        candidate_episode_ids: list[str] | None = None,
        *,
        allow_ignore_latest: bool = False,
    ) -> tuple[int, int]:
        expected = set(turn_ids)
        allowed_episodes = set(candidate_episode_ids or [])
        if not expected or len(expected) != len(turn_ids):
            raise ValueError("invalid consolidation turn coverage")
        covered: set[str] = set()
        now = time.time()
        linked = 0
        deferred = 0
        touched_episodes: set[str] = set()
        with self._db:
            for decision in decisions:
                if not isinstance(decision, dict):
                    raise ValueError("invalid consolidation decision")
                action = str(decision.get("action") or "")
                expected_keys = {
                    "defer": {"action", "turn_ids", "reason"},
                    "ignore": {"action", "turn_ids", "reason"},
                    "continue": {
                        "action",
                        "episode_id",
                        "turn_ids",
                        "topics",
                        "entities",
                        "open_loops",
                        "salience",
                    },
                    "new": {
                        "action",
                        "key",
                        "title",
                        "turn_ids",
                        "topics",
                        "entities",
                        "open_loops",
                        "salience",
                    },
                }.get(action)
                if expected_keys is None or set(decision) != expected_keys:
                    raise ValueError("invalid consolidation decision")
                raw_turns = decision["turn_ids"]
                if not isinstance(raw_turns, list) or any(
                    not isinstance(value, str) for value in raw_turns
                ):
                    raise ValueError("invalid consolidation turn coverage")
                decision_turns = [str(value) for value in raw_turns]
                if (
                    not decision_turns
                    or len(decision_turns) != len(set(decision_turns))
                    or not set(decision_turns) <= expected
                    or covered & set(decision_turns)
                ):
                    raise ValueError("invalid consolidation turn coverage")
                covered.update(decision_turns)
                if action == "defer":
                    if decision_turns != [turn_ids[-1]]:
                        raise ValueError("only latest consolidation turn may defer")
                    self._upsert_consolidation_decision(
                        decision_turns[0],
                        "deferred",
                        str(decision.get("reason") or ""),
                        now,
                    )
                    deferred += 1
                    continue
                if action == "ignore":
                    if turn_ids[-1] in decision_turns and not allow_ignore_latest:
                        raise ValueError("latest consolidation turn may not be ignored")
                    for turn_id in decision_turns:
                        self._upsert_consolidation_decision(
                            turn_id,
                            "ignored",
                            str(decision.get("reason") or ""),
                            now,
                        )
                    continue
                topics = self._consolidation_strings(
                    decision["topics"], "topics", 12, 200
                )
                entities = self._consolidation_strings(
                    decision["entities"], "entities", 20, 200
                )
                loops = self._consolidation_strings(
                    decision["open_loops"], "open loops", 8, 500
                )
                salience = decision["salience"]
                if (
                    isinstance(salience, bool)
                    or not isinstance(salience, (int, float))
                    or not 0 <= float(salience) <= 1
                ):
                    raise ValueError("invalid consolidation salience")
                if action == "continue":
                    episode_id = str(decision["episode_id"])
                    if (
                        episode_id not in allowed_episodes
                        or self.episode(episode_id) is None
                    ):
                        raise ValueError("unknown consolidation episode")
                    archive_kind = self._runtime_archive_kind(episode_id)
                    if archive_kind:
                        raise ValueError(
                            f"{archive_kind} archive does not accept owner turns"
                        )
                else:
                    key = str(decision["key"])
                    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", key):
                        raise ValueError("invalid consolidation episode key")
                    episode_id = uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"momoi:consolidated-episode:{decision_turns[0]}:{key}",
                    ).hex
                    title = str(decision["title"]).strip()
                    if not title or len(title) > 200:
                        raise ValueError("invalid consolidation title")
                raw_text = "\n".join(
                    str(row["content"])
                    for turn_id in decision_turns
                    for row in self._db.execute(
                        """SELECT content FROM messages
                           WHERE turn_id=? ORDER BY id""",
                        (turn_id,),
                    ).fetchall()
                )
                existing = self.episode(episode_id)
                if existing is not None:
                    episode_id = self._roll_episode(
                        episode_id,
                        decision_turns[0],
                        now,
                        raw_text,
                        incoming_turns=len(decision_turns),
                    )
                    existing = self.episode(episode_id)
                status = "open" if loops else "closing"
                if existing is None:
                    self._db.execute(
                        """INSERT INTO conversation_episodes
                           (id, status, title, topics_json, entities_json,
                            open_loops_json, salience, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            episode_id,
                            status,
                            title,
                            json.dumps(topics, ensure_ascii=False),
                            json.dumps(entities, ensure_ascii=False),
                            json.dumps(loops, ensure_ascii=False),
                            float(salience),
                            now,
                            now,
                        ),
                    )
                else:
                    merged_topics = list(
                        dict.fromkeys([*existing["topics"], *topics])
                    )[:12]
                    merged_entities = list(
                        dict.fromkeys([*existing["entities"], *entities])
                    )[:20]
                    self._db.execute(
                        """UPDATE conversation_episodes
                           SET topics_json=?, entities_json=?,
                               open_loops_json=?, salience=MAX(salience, ?),
                               status=?, closed_at=NULL, updated_at=?
                           WHERE id=?""",
                        (
                            json.dumps(merged_topics, ensure_ascii=False),
                            json.dumps(merged_entities, ensure_ascii=False),
                            json.dumps(loops, ensure_ascii=False),
                            float(salience),
                            status,
                            now,
                            episode_id,
                        ),
                    )
                for turn_id in decision_turns:
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
                           VALUES (?, ?, ?, 'primary', '[]')""",
                        (episode_id, turn_id, ordinal),
                    )
                    self._upsert_consolidation_decision(
                        turn_id, "linked", "", now, episode_id
                    )
                    self._index_turn_episode_terms(turn_id)
                    linked += 1
                touched_episodes.add(episode_id)
            if covered != expected:
                raise ValueError("incomplete consolidation turn coverage")
            for episode_id in touched_episodes:
                self._reorder_episode_turns(episode_id, now)
                self._reindex_episode_terms(episode_id)
        return linked, deferred

    @staticmethod
    def _consolidation_strings(
        value: object, name: str, maximum: int, max_length: int
    ) -> list[str]:
        if (
            not isinstance(value, list)
            or len(value) > maximum
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item.strip()) > max_length
                for item in value
            )
        ):
            raise ValueError(f"invalid consolidation {name}")
        return [str(item).strip() for item in value]

    def claim_episode_annealing_candidate(
        self, raw_tail_turns: int, raw_token_budget: int
    ) -> dict[str, object] | None:
        raw_tail_turns = max(1, raw_tail_turns)
        raw_token_budget = max(1, raw_token_budget)
        now = time.time()
        with self._db:
            episodes = self._db.execute(
                """SELECT * FROM conversation_episodes
                   WHERE summary_claimed_at IS NULL
                     AND summary_abandoned_at IS NULL
                     AND COALESCE(summary_retry_at, 0)<=?
                     AND NOT EXISTS (
                         SELECT 1 FROM episode_turns AS et
                         JOIN messages AS m ON m.turn_id=et.turn_id
                         WHERE et.episode_id=conversation_episodes.id
                           AND m.delivery_state='queued'
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM episode_turns AS waiting_turn
                         JOIN self_state AS state
                           ON state.pending_reply_turn_id=waiting_turn.turn_id
                         WHERE waiting_turn.episode_id=conversation_episodes.id
                           AND state.id=1
                           AND state.pending_reply_expectation<>''
                     )
                   ORDER BY updated_at""",
                (now,),
            ).fetchall()
            for episode in episodes:
                rows = self._db.execute(
                    """SELECT et.ordinal, m.id, m.turn_id, m.role, m.content,
                              m.created_at, m.delivery_state
                       FROM episode_turns AS et
                       JOIN messages AS m ON m.turn_id=et.turn_id
                       WHERE et.episode_id=? AND et.ordinal>?
                         AND (m.role IN ('user', 'event') OR m.delivery_state IN
                              ('delivered', 'uncertain', 'internal'))
                       ORDER BY et.ordinal, m.id""",
                    (
                        episode["id"],
                        episode["summarized_through_ordinal"],
                    ),
                ).fetchall()
                ordinals = list(dict.fromkeys(int(row["ordinal"]) for row in rows))
                tail_turns = raw_tail_turns if episode["status"] != "closed" else 0
                if not rows and episode["narrative_summary"]:
                    continue
                if not rows and episode["working_summary_claims_json"] != "[]":
                    cursor = self._db.execute(
                        """UPDATE conversation_episodes SET summary_claimed_at=?
                           WHERE id=? AND summary_claimed_at IS NULL""",
                        (now, episode["id"]),
                    )
                    if cursor.rowcount == 1:
                        return {
                            "episode": self._episode_dict(episode),
                            "through_ordinal": int(
                                episode["summarized_through_ordinal"]
                            ),
                            "messages": [],
                        }
                    continue
                if len(ordinals) <= tail_turns:
                    continue
                tokens = sum(estimate_tokens(str(row["content"])) for row in rows)
                if (
                    episode["status"] != "closed"
                    and len(ordinals) <= raw_tail_turns * 2
                    and tokens <= math.ceil(raw_token_budget * 1.25)
                ):
                    continue
                compact: list[dict[str, object]] = []
                compact_tokens = 0
                through = 0
                selected_ordinals = (
                    ordinals[:-tail_turns] if tail_turns else ordinals
                )
                for ordinal in selected_ordinals:
                    group = [row for row in rows if int(row["ordinal"]) == ordinal]
                    group_tokens = sum(
                        estimate_tokens(str(row["content"])) for row in group
                    )
                    if group_tokens > raw_token_budget and not compact:
                        break
                    if compact and compact_tokens + group_tokens > raw_token_budget:
                        break
                    for row in group:
                        item = dict(row)
                        item["timestamp"] = context_timestamp(item["created_at"])
                        compact.append(item)
                    compact_tokens += group_tokens
                    through = ordinal
                if not compact:
                    continue
                cursor = self._db.execute(
                    """UPDATE conversation_episodes SET summary_claimed_at=?
                       WHERE id=? AND summary_claimed_at IS NULL""",
                    (now, episode["id"]),
                )
                if cursor.rowcount != 1:
                    continue
                return {
                    "episode": self._episode_dict(episode),
                    "through_ordinal": through,
                    "messages": compact,
                }
        return None

    def finish_episode_annealing(
        self,
        episode_id: str,
        through_ordinal: int,
        claims: list[object],
        *,
        narrative_summary: str = "",
        emotional_context: dict[str, object] | None = None,
        outcomes: list[object] | None = None,
    ) -> str:
        if not 1 <= len(claims) <= 64:
            raise ValueError("episode summary needs 1 to 64 evidence claims")
        normalized: list[dict[str, object]] = []
        seen: set[tuple[int, str]] = set()
        for claim in claims:
            if not isinstance(claim, dict) or set(claim) != {
                "message_id",
                "turn_id",
                "ordinal",
                "quote",
            }:
                raise ValueError("invalid episode summary claim")
            message_id = claim["message_id"]
            ordinal = claim["ordinal"]
            if (
                isinstance(message_id, bool)
                or not isinstance(message_id, int)
                or isinstance(ordinal, bool)
                or not isinstance(ordinal, int)
                or not isinstance(claim["turn_id"], str)
                or not isinstance(claim["quote"], str)
            ):
                raise ValueError("invalid episode summary citation")
            quote = str(claim["quote"]).strip()
            if not quote or len(quote) > 1000:
                raise ValueError("invalid episode summary quote")
            row = self._db.execute(
                """SELECT m.turn_id, et.ordinal, m.role, m.content,
                          m.delivery_state
                   FROM episode_turns AS et
                   JOIN messages AS m ON m.turn_id=et.turn_id
                   WHERE et.episode_id=? AND m.id=?""",
                (episode_id, message_id),
            ).fetchone()
            if (
                row is None
                or str(row["turn_id"]) != claim["turn_id"]
                or int(row["ordinal"]) != ordinal
                or ordinal > through_ordinal
                or quote not in str(row["content"])
                or (
                    row["role"] == "assistant"
                    and row["delivery_state"]
                    not in {"delivered", "uncertain", "internal"}
                )
            ):
                raise ValueError("episode summary evidence does not match raw history")
            key = (message_id, quote)
            if key in seen:
                raise ValueError("duplicate episode summary claim")
            seen.add(key)
            normalized.append(
                {
                    "message_id": message_id,
                    "turn_id": str(row["turn_id"]),
                    "ordinal": int(row["ordinal"]),
                    "role": str(row["role"]),
                    "delivery_state": str(row["delivery_state"]),
                    "quote": quote,
                }
            )
        lines = []
        for claim in normalized:
            if claim["role"] == "user":
                source = "OWNER"
            elif claim["delivery_state"] == "uncertain":
                source = "MOMOI delivery=uncertain"
            elif claim["delivery_state"] == "internal":
                source = "MOMOI visibility=internal"
            else:
                source = "MOMOI delivery=delivered"
            lines.append(
                f"- [source {source} turn={claim['turn_id']} "
                f"ordinal={claim['ordinal']}] "
                f"{json.dumps(claim['quote'], ensure_ascii=False)}"
            )
        working_summary = "\n".join(lines)
        if len(working_summary) > 12000:
            raise ValueError("episode summary exceeds storage budget")
        narrative_summary = narrative_summary.strip()
        if len(narrative_summary) > 800:
            raise ValueError("episode narrative exceeds storage budget")
        emotional_context = emotional_context or {}
        if (
            not isinstance(emotional_context, dict)
            or set(emotional_context) - {"owner", "momoi", "tone"}
            or any(
                not isinstance(value, str) or len(value.strip()) > 300
                for value in emotional_context.values()
            )
        ):
            raise ValueError("invalid episode emotional context")
        outcomes = outcomes or []
        if (
            not isinstance(outcomes, list)
            or len(outcomes) > 12
            or any(
                not isinstance(value, str)
                or not value.strip()
                or len(value.strip()) > 500
                for value in outcomes
            )
        ):
            raise ValueError("invalid episode outcomes")
        with self._db:
            cursor = self._db.execute(
                """UPDATE conversation_episodes
                   SET working_summary=?, working_summary_claims_json=?,
                       narrative_summary=?, emotional_context_json=?,
                       outcomes_json=?,
                       summarized_through_ordinal=?,
                       summary_claimed_at=NULL, summary_retry_at=NULL,
                       summary_failure_count=0, summary_abandoned_at=NULL,
                       updated_at=?
                   WHERE id=? AND summary_claimed_at IS NOT NULL
                     AND summarized_through_ordinal<=?""",
                (
                    working_summary,
                    json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                    narrative_summary,
                    json.dumps(
                        {
                            key: str(value).strip()
                            for key, value in emotional_context.items()
                            if str(value).strip()
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        [str(value).strip() for value in outcomes],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    through_ordinal,
                    time.time(),
                    episode_id,
                    through_ordinal,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("claimed episode summary was not found")
            self._reindex_episode_terms(episode_id)
        return working_summary

    def release_episode_annealing(
        self, episode_id: str, *, failed: bool = True
    ) -> None:
        with self._db:
            if not failed:
                self._db.execute(
                    """UPDATE conversation_episodes
                       SET summary_claimed_at=NULL
                       WHERE id=? AND summary_claimed_at IS NOT NULL""",
                    (episode_id,),
                )
                return
            row = self._db.execute(
                """SELECT summary_failure_count FROM conversation_episodes
                   WHERE id=? AND summary_claimed_at IS NOT NULL""",
                (episode_id,),
            ).fetchone()
            if row is None:
                return
            failures = int(row["summary_failure_count"]) + 1
            if failures >= EPISODE_ANNEAL_MAX_FAILURES:
                self._db.execute(
                    """UPDATE conversation_episodes
                       SET summary_claimed_at=NULL, summary_retry_at=NULL,
                           summary_failure_count=?, summary_abandoned_at=?
                       WHERE id=?""",
                    (failures, time.time(), episode_id),
                )
                log_event(
                    logger,
                    logging.WARNING,
                    "episode_anneal_abandoned",
                    episode_id=episode_id,
                    failures=failures,
                )
                return
            delay = min(3600, 60 * 2 ** min(failures - 1, 6))
            self._db.execute(
                """UPDATE conversation_episodes
                   SET summary_claimed_at=NULL, summary_retry_at=?,
                       summary_failure_count=? WHERE id=?""",
                (time.time() + delay, failures, episode_id),
            )

    def next_episode_annealing_retry_at(self) -> float | None:
        row = self._db.execute(
            """SELECT MIN(summary_retry_at) AS due
               FROM conversation_episodes
               WHERE summary_claimed_at IS NULL
                 AND summary_abandoned_at IS NULL
                 AND summary_retry_at IS NOT NULL"""
        ).fetchone()
        return float(row["due"]) if row and row["due"] is not None else None

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
                salience, created_at, updated_at)
               VALUES (?, 'closing', ?, ?, ?, ?, ?, ?, ?)""",
            (
                successor,
                row["title"],
                row["topics_json"],
                row["entities_json"],
                row["open_loops_json"],
                row["salience"],
                now,
                now,
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
        plan = json.loads(str(row["plan_json"])) if row is not None else {}
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
                      AND NOT EXISTS (
                          SELECT 1 FROM episode_turns AS archive_turn
                          WHERE archive_turn.episode_id=conversation_episodes.id
                            AND (
                                archive_turn.turn_id GLOB 'webhook:*'
                                OR EXISTS (
                                    SELECT 1 FROM turns AS archive_source
                                    WHERE archive_source.id=archive_turn.turn_id
                                      AND archive_source.kind='autonomous'
                                      AND EXISTS (
                                          SELECT 1
                                          FROM json_each(
                                              archive_source.source_ids_json
                                          ) AS source_id
                                          WHERE source_id.value GLOB 'heartbeat:*'
                                      )
                                )
                            )
                      )
                      AND id IN (
                          SELECT et.episode_id FROM episode_turns AS et
                          JOIN turns AS t ON t.id=et.turn_id WHERE t.kind='owner'
                      )""",
                (now, now, *selected),
            )
        elif not keep_open:
            self._db.execute(
                """UPDATE conversation_episodes SET status='closed',
                   closed_at=?, updated_at=?
                   WHERE status='closing'
                     AND NOT EXISTS (
                         SELECT 1 FROM episode_turns AS archive_turn
                         WHERE archive_turn.episode_id=conversation_episodes.id
                           AND (
                               archive_turn.turn_id GLOB 'webhook:*'
                               OR EXISTS (
                                   SELECT 1 FROM turns AS archive_source
                                   WHERE archive_source.id=archive_turn.turn_id
                                     AND archive_source.kind='autonomous'
                                     AND EXISTS (
                                         SELECT 1
                                         FROM json_each(
                                             archive_source.source_ids_json
                                         ) AS source_id
                                         WHERE source_id.value GLOB 'heartbeat:*'
                                     )
                               )
                           )
                     )
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

    def self_state(self) -> dict[str, object]:
        row = self._db.execute("SELECT * FROM self_state WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("self_state is not initialized")
        return dict(row)

    def self_state_context(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        state = self.self_state()

        def timestamp(value: object) -> str | None:
            return context_timestamp(value) if value is not None else None

        return json.dumps(
            {
                "mood": {
                    "state": state["mood_state"],
                    "intensity": state["mood_intensity"],
                    "cause": state["mood_cause"],
                    "updated_at": timestamp(state["mood_updated_at"]),
                    "age_minutes": max(
                        0, int((now - float(state["mood_updated_at"])) / 60)
                    ),
                },
                "activity": {
                    "text": state["activity"],
                    "result": state["activity_result"],
                    "since": timestamp(state["activity_since"]),
                },
                "last_heartbeat_at": timestamp(state["last_heartbeat_at"]),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def recent_heartbeat_activities(self) -> list[dict[str, str]]:
        rows = self._db.execute(
            """SELECT content, created_at FROM messages
               WHERE delivery_state='internal'
                 AND content LIKE '[AUTONOMOUS HEARTBEAT RECORD;%'
               ORDER BY created_at DESC, id DESC
               LIMIT ?""",
            (RECENT_HEARTBEAT_LIMIT,),
        ).fetchall()
        items: list[dict[str, str]] = []
        for row in reversed(rows):
            text = _heartbeat_record_activity(str(row["content"] or ""))
            if not text:
                continue
            items.append(
                {
                    "at": context_timestamp(row["created_at"]),
                    "text": text,
                }
            )
        return items[-RECENT_HEARTBEAT_LIMIT:]

    def pending_owner_reply(self, now: float | None = None) -> dict[str, object] | None:
        now = time.time() if now is None else now
        row = self._db.execute(
            """SELECT pending_reply_turn_id, pending_reply_expectation,
                      pending_reply_since, pending_reply_last_reason,
                      pending_reply_channel, pending_reply_delay_minutes,
                      pending_reply_next_check_at
               FROM self_state WHERE id=1"""
        ).fetchone()
        if row is None or not str(row["pending_reply_expectation"] or "").strip():
            return None
        since = float(row["pending_reply_since"] or now)
        source_turn = str(row["pending_reply_turn_id"] or "")
        source_messages = [
            {
                "role": str(item["role"]),
                "content": str(item["content"]),
                "delivery_state": str(item["delivery_state"]),
                "timestamp": context_timestamp(item["created_at"]),
            }
            for item in self._db.execute(
                """SELECT role, content, created_at, delivery_state FROM messages
                   WHERE turn_id=? AND (role IN ('user', 'event') OR delivery_state IN ('delivered','uncertain'))
                   ORDER BY id""",
                (source_turn,),
            ).fetchall()
        ]
        return {
            "source_turn": source_turn,
            "source_messages": source_messages,
            "expected_information": str(row["pending_reply_expectation"]),
            "reason": str(row["pending_reply_last_reason"] or ""),
            "waiting_since": context_timestamp(since),
            "waiting_minutes": max(0, int((now - since) / 60)),
            "delay_minutes": int(row["pending_reply_delay_minutes"] or 0),
            "deadline": context_timestamp(row["pending_reply_next_check_at"] or now),
            "channel": str(row["pending_reply_channel"] or ""),
        }

    def _apply_mood_update(
        self, update: dict[str, object] | None, now: float
    ) -> None:
        if update is None:
            return
        previous = self._db.execute(
            "SELECT mood_state, mood_intensity FROM self_state WHERE id=1"
        ).fetchone()
        self._db.execute(
            """UPDATE self_state
               SET mood_state=?, mood_intensity=?, mood_cause=?,
                   mood_updated_at=?, updated_at=? WHERE id=1""",
            (
                update["state"],
                update["intensity"],
                str(update["cause"])[:300],
                now,
                now,
            ),
        )
        log_event(
            logger,
            logging.DEBUG,
            "mood_changed",
            previous_state=previous["mood_state"] if previous else "unknown",
            state=update["state"],
            intensity=round(float(update["intensity"]), 2),
            cause=safe_preview(update["cause"], 300),
        )

    def ensure_heartbeat(
        self, config: HeartbeatConfig, now: float | None = None
    ) -> None:
        if not config.enabled:
            return
        now = time.time() if now is None else now
        with self._db:
            self._db.execute(
                """UPDATE self_state SET next_heartbeat_at=?, updated_at=?
                   WHERE id=1 AND next_heartbeat_at<=0""",
                (now + config.initial_delay_seconds, now),
            )

    def claim_due_heartbeat(
        self,
        config: HeartbeatConfig,
        notifications: NotificationConfig,
        now: float | None = None,
    ) -> dict[str, object] | None:
        now = time.time() if now is None else now
        with self._db:
            row = self._db.execute("SELECT * FROM self_state WHERE id=1").fetchone()
            if row is None or row["heartbeat_claimed_at"] is not None:
                return None
            waiting = bool(str(row["pending_reply_expectation"] or "").strip())
            due: list[tuple[float, str]] = []
            if config.enabled and float(row["next_heartbeat_at"] or 0) > 0:
                due.append((float(row["next_heartbeat_at"]), "ordinary"))
            if waiting and row["pending_reply_next_check_at"] is not None:
                due.append((float(row["pending_reply_next_check_at"]), "reply"))
            if not due:
                return None
            scheduled_at, claim_kind = min(
                due, key=lambda item: (item[0], item[1] != "reply")
            )
            if scheduled_at > now:
                return None
            quiet_end = quiet_until(now, notifications)
            if quiet_end > now:
                column = (
                    "pending_reply_next_check_at"
                    if claim_kind == "reply"
                    else "next_heartbeat_at"
                )
                self._db.execute(
                    f"UPDATE self_state SET {column}=?, updated_at=? WHERE id=1",
                    (quiet_end, now),
                )
                return None
            self._db.execute(
                """UPDATE self_state SET heartbeat_claimed_at=?,
                   heartbeat_claim_kind=? WHERE id=1""",
                (now, claim_kind),
            )
        claimed = dict(row)
        claimed["heartbeat_claim_kind"] = claim_kind
        claimed["heartbeat_scheduled_at"] = scheduled_at
        return claimed

    def claim_manual_heartbeat(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._db:
            row = self._db.execute(
                "SELECT heartbeat_claimed_at FROM self_state WHERE id=1"
            ).fetchone()
            if row is None or row["heartbeat_claimed_at"] is not None:
                return False
            self._db.execute(
                """UPDATE self_state SET heartbeat_claimed_at=?,
                   heartbeat_claim_kind='manual', updated_at=? WHERE id=1""",
                (now, now),
            )
        return True

    def next_heartbeat_due_at(self, enabled: bool) -> float | None:
        row = self._db.execute(
            """SELECT next_heartbeat_at, pending_reply_expectation,
                      pending_reply_next_check_at FROM self_state
               WHERE id=1 AND heartbeat_claimed_at IS NULL"""
        ).fetchone()
        if row is None:
            return None
        waiting = bool(str(row["pending_reply_expectation"] or "").strip())
        due: list[float] = []
        if enabled and float(row["next_heartbeat_at"] or 0) > 0:
            due.append(float(row["next_heartbeat_at"]))
        if waiting and row["pending_reply_next_check_at"] is not None:
            due.append(float(row["pending_reply_next_check_at"]))
        return min(due) if due else None

    def release_heartbeat_claim(self, delay_seconds: float) -> None:
        now = time.time()
        with self._db:
            state = self._db.execute(
                """SELECT heartbeat_claim_kind, pending_reply_expectation
                   FROM self_state WHERE id=1"""
            ).fetchone()
            if (
                state
                and state["heartbeat_claim_kind"] == "reply"
                and str(state["pending_reply_expectation"] or "").strip()
            ):
                self._db.execute(
                    """UPDATE self_state SET heartbeat_claimed_at=NULL,
                       heartbeat_claim_kind=NULL, pending_reply_next_check_at=?,
                       updated_at=? WHERE id=1""",
                    (now + delay_seconds, now),
                )
            elif state and state["heartbeat_claim_kind"] == "reply":
                self._db.execute(
                    """UPDATE self_state SET heartbeat_claimed_at=NULL,
                       heartbeat_claim_kind=NULL, updated_at=? WHERE id=1""",
                    (now,),
                )
            else:
                self._db.execute(
                    """UPDATE self_state SET heartbeat_claimed_at=NULL,
                       heartbeat_claim_kind=NULL, next_heartbeat_at=?, updated_at=?
                       WHERE id=1""",
                    (now + delay_seconds, now),
                )

    def clear_heartbeat_claim(self) -> None:
        with self._db:
            self._db.execute(
                """UPDATE self_state SET heartbeat_claimed_at=NULL,
                   heartbeat_claim_kind=NULL WHERE id=1"""
            )

    def commit_reply_followup(
        self,
        turn_id: str,
        *,
        owner_event_revision: int,
        notification_config: NotificationConfig,
        pending_reply_turn_id: str,
        reason: str,
        mood_update: dict[str, object] | None,
        notification_channel: str = "",
    ) -> int:
        state = self.self_state()
        return self._commit_scheduled_turn(
            turn_id,
            owner_event_revision=owner_event_revision,
            notification_config=notification_config,
            activity=str(state["activity"]),
            result=str(state.get("activity_result") or ""),
            next_heartbeat_at=float(state["next_heartbeat_at"]),
            mood_update=mood_update,
            messages=[],
            reason=reason,
            pending_reply_turn_id=pending_reply_turn_id,
            notification_channel=notification_channel,
            reply_followup_only=True,
        )

    def commit_heartbeat(
        self,
        turn_id: str,
        *,
        owner_event_revision: int,
        notification_config: NotificationConfig,
        activity: str,
        result: str,
        next_heartbeat_at: float,
        mood_update: dict[str, object] | None,
        messages: list[ChannelMessage],
        reason: str,
        reply_expectation: str = "",
        reply_wait_minutes: int = 0,
        reply_wait_reason: str = "",
        draft: TurnDraft | None = None,
        memory_events: list[IncomingMessage] | None = None,
        notification_channel: str = "",
    ) -> int:
        return self._commit_scheduled_turn(
            turn_id,
            owner_event_revision=owner_event_revision,
            notification_config=notification_config,
            activity=activity,
            result=result,
            next_heartbeat_at=next_heartbeat_at,
            mood_update=mood_update,
            messages=messages,
            reason=reason,
            reply_expectation=reply_expectation,
            reply_wait_minutes=reply_wait_minutes,
            reply_wait_reason=reply_wait_reason,
            draft=draft,
            memory_events=memory_events,
            notification_channel=notification_channel,
        )

    def _commit_scheduled_turn(
        self,
        turn_id: str,
        *,
        owner_event_revision: int,
        notification_config: NotificationConfig,
        activity: str,
        result: str,
        next_heartbeat_at: float,
        mood_update: dict[str, object] | None,
        messages: list[ChannelMessage],
        reason: str,
        reply_expectation: str = "",
        reply_wait_minutes: int = 0,
        reply_wait_reason: str = "",
        draft: TurnDraft | None = None,
        memory_events: list[IncomingMessage] | None = None,
        pending_reply_turn_id: str | None = None,
        notification_channel: str = "",
        reply_followup_only: bool = False,
    ) -> int:
        now = time.time()
        current = self.self_state()
        with self._db:
            pending = self._db.execute(
                """SELECT pending_reply_turn_id, pending_reply_channel
                   FROM self_state WHERE id=1"""
            ).fetchone()
            pending_reply_is_current = bool(
                pending_reply_turn_id
                and pending
                and pending["pending_reply_turn_id"] == pending_reply_turn_id
            )
            if pending_reply_turn_id and not pending_reply_is_current:
                messages = []
            conversation = self.heartbeat_conversation_snapshot()
            if (
                int(conversation["owner_event_revision"]) != owner_event_revision
                or conversation["owner_busy"]
            ):
                messages = []
            notification_key = (
                "heartbeat.reply_followup"
                if pending_reply_is_current
                else "heartbeat.chat"
            )
            if not self.heartbeat_contact_window(
                notification_key,
                notification_config,
                now,
                apply_cooldown=not pending_reply_is_current,
            )["allowed"]:
                messages = []
            source_json = json.dumps(
                [f"{'reply-followup' if reply_followup_only else 'heartbeat'}:{turn_id}"]
            )
            progress_rows = self._db.execute(
                """SELECT p.text, p.created_at, p.tool_call_id, p.part_index,
                          o.id AS outbox_id, o.state, o.possible_duplicate,
                          o.target_channel
                   FROM turn_progress AS p
                   LEFT JOIN outbox AS o
                     ON o.dedupe_key = 'turn:' || p.turn_id || ':progress:' ||
                        p.tool_call_id || ':' || p.part_index
                   WHERE p.turn_id=?
                   ORDER BY p.created_at, p.tool_call_id, p.part_index""",
                (turn_id,),
            ).fetchall()
            progress_rows = [
                row
                for row in progress_rows
                if row["outbox_id"] is not None
                and str(row["state"] or "") != "superseded"
            ]
            message_turn_id = (
                str(pending_reply_turn_id)
                if reply_followup_only and pending_reply_is_current
                else turn_id
            )
            for row in progress_rows:
                if row["outbox_id"] is None:
                    continue
                self._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at, source_event_ids_json,
                        outbox_id, delivery_state)
                       SELECT ?, 'assistant', ?, ?, ?, ?, ?
                       WHERE NOT EXISTS (
                           SELECT 1 FROM messages WHERE outbox_id=?
                       )""",
                    (
                        message_turn_id,
                        row["text"],
                        row["created_at"],
                        source_json,
                        row["outbox_id"],
                        self._message_delivery_state(
                            str(row["state"]), bool(row["possible_duplicate"])
                        ),
                        row["outbox_id"],
                    ),
                )
            if pending_reply_is_current:
                delivery_states = {str(row["state"] or "") for row in progress_rows}
                followup_delivered = "sent" in delivery_states
                followup_failed = bool(
                    delivery_states
                    and delivery_states <= {"failed", "superseded"}
                )
                if reply_followup_only and not (
                    followup_delivered or followup_failed
                ):
                    self._db.execute(
                        """UPDATE self_state SET pending_reply_next_check_at=NULL
                           WHERE id=1"""
                    )
                else:
                    self._db.execute(
                        """UPDATE self_state SET pending_reply_turn_id=NULL,
                           pending_reply_expectation='', pending_reply_since=NULL,
                           pending_reply_checks=0, pending_reply_last_reason='',
                           pending_reply_channel='', pending_reply_delay_minutes=0,
                           pending_reply_next_check_at=NULL
                           WHERE id=1"""
                    )
                if reply_followup_only and not followup_failed:
                    self._db.execute(
                        "UPDATE turns SET updated_at=? WHERE id=?",
                        (now, pending_reply_turn_id),
                    )
                    episode_ids = [
                        str(row["episode_id"])
                        for row in self._db.execute(
                            """SELECT episode_id FROM episode_turns
                               WHERE turn_id=?""",
                            (pending_reply_turn_id,),
                        ).fetchall()
                    ]
                    for episode_id in episode_ids:
                        if self._runtime_archive_kind(episode_id):
                            continue
                        self._db.execute(
                            """UPDATE conversation_episodes
                               SET status=CASE
                                     WHEN open_loops_json='[]' THEN 'closing'
                                     ELSE status
                                   END,
                                   closed_at=NULL,
                                   working_summary='',
                                   working_summary_claims_json='[]',
                                   narrative_summary='',
                                   emotional_context_json='{}',
                                   outcomes_json='[]',
                                   summarized_through_ordinal=0,
                                   summary_claimed_at=NULL,
                                   summary_retry_at=NULL,
                                   summary_failure_count=0,
                                   summary_abandoned_at=NULL,
                                   updated_at=?
                               WHERE id=?""",
                            (now, episode_id),
                        )
                        self._reindex_episode_terms(episode_id)
                    self._index_turn_episode_terms(str(pending_reply_turn_id))
            if not reply_followup_only:
                self._apply_cooled_reply_action(draft, now)
            self._apply_mood_update(mood_update, now)
            if reply_followup_only:
                self._db.execute(
                    """UPDATE self_state SET heartbeat_claimed_at=NULL,
                       heartbeat_claim_kind=NULL, updated_at=? WHERE id=1""",
                    (now,),
                )
            else:
                self._apply_goal_mutations(draft, now)
                for memory in draft.memories if draft else []:
                    self._remember(memory, memory_events or [], now)
                for forgotten in draft.forgotten_memories if draft else []:
                    self._forget_memory(forgotten, memory_events or [], now)
                activity_since = (
                    current["activity_since"]
                    if current["activity"] == activity
                    else now
                )
                self._db.execute(
                    """UPDATE self_state SET activity=?, activity_result=?,
                       activity_since=?, last_heartbeat_at=?, next_heartbeat_at=?,
                       heartbeat_claimed_at=NULL, heartbeat_claim_kind=NULL,
                       updated_at=? WHERE id=1""",
                    (
                        activity,
                        result[:2000],
                        activity_since,
                        now,
                        next_heartbeat_at,
                        now,
                    ),
                )
                heartbeat_record = (
                    "[AUTONOMOUS HEARTBEAT RECORD; not sent to the owner]\n"
                    f"Activity: {activity}\n"
                    f"Result: {result.strip() or '(no concrete result recorded)'}"
                )
                heartbeat_source = json.dumps([f"heartbeat-record:{turn_id}"])
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
                        heartbeat_record,
                        now,
                        heartbeat_source,
                        turn_id,
                        heartbeat_source,
                    ),
                )
                episode_key, title = self._heartbeat_day_episode(now)
                self._ensure_autonomous_episode(
                    episode_key,
                    turn_id,
                    title,
                    now,
                    activity,
                    result,
                    *(str(row["text"]) for row in progress_rows),
                )
            target_channel = (
                str(pending["pending_reply_channel"] or "")
                if pending_reply_is_current
                else notification_channel
            )
            if progress_rows and not pending_reply_is_current:
                target_channel = str(
                    progress_rows[-1]["target_channel"] or target_channel
                )
            if progress_rows:
                normalized = [self._outbox_content(message) for message in messages]
                for index, (text, kind, path, payload) in enumerate(normalized):
                    self._db.execute(
                        """INSERT OR IGNORE INTO outbox
                           (turn_id, dedupe_key, text, kind, media_path, payload_json,
                            reply_expectation, target_channel)
                           VALUES (?, ?, ?, ?, ?, ?, '', ?)""",
                        (
                            turn_id,
                            f"turn:{turn_id}:final:{index}",
                            text,
                            kind,
                            path,
                            json.dumps(
                                payload, ensure_ascii=False, separators=(",", ":")
                            ),
                            target_channel,
                        ),
                    )
                    outbox = self._db.execute(
                        "SELECT id FROM outbox WHERE dedupe_key=?",
                        (f"turn:{turn_id}:final:{index}",),
                    ).fetchone()
                    self._db.execute(
                        """INSERT OR IGNORE INTO messages
                           (turn_id, role, content, created_at, source_event_ids_json,
                            outbox_id, delivery_state)
                           VALUES (?, 'assistant', ?, ?, ?, ?, 'queued')""",
                        (
                            turn_id,
                            text,
                            now,
                            source_json,
                            outbox["id"],
                        ),
                    )
                visible = [str(row["text"]) for row in progress_rows] + [
                    text for text, _, _, _ in normalized
                ]
                self._db.execute(
                    """INSERT OR IGNORE INTO notifications
                       (id, turn_id, goal_id, notification_key, priority, reason,
                        messages_json, reply_expectation, state, not_before, created_at,
                        queued_at, target_channel)
                       VALUES (?, ?, 'heartbeat', ?, 'normal', ?, ?, ?,
                               'queued', ?, ?, ?, ?)""",
                    (
                        f"notification:{turn_id}",
                        turn_id,
                        notification_key,
                        reason[:500],
                        json.dumps(visible, ensure_ascii=False),
                        (
                            encode_reply_wait(
                                reply_expectation,
                                reply_wait_reason,
                                reply_wait_minutes,
                            )
                            if reply_expectation
                            else ""
                        ),
                        now,
                        now,
                        now,
                        target_channel,
                    ),
                )
                if reply_expectation:
                    self._bind_turn_reply_expectation(
                        turn_id,
                        reply_expectation,
                        reply_wait_minutes,
                        reply_wait_reason,
                    )
            elif messages:
                self._db.execute(
                    """INSERT OR IGNORE INTO notifications
                       (id, turn_id, goal_id, notification_key, priority, reason,
                        messages_json, reply_expectation, state, not_before, created_at,
                        claimed_at, target_channel)
                        VALUES (?, ?, 'heartbeat', ?, 'normal', ?, ?, ?,
                               'pending', ?, ?, ?, ?)""",
                    (
                        f"notification:{turn_id}",
                        turn_id,
                        notification_key,
                        reason[:500],
                        json.dumps(messages, ensure_ascii=False),
                        (
                            encode_reply_wait(
                                reply_expectation,
                                reply_wait_reason,
                                reply_wait_minutes,
                            )
                            if reply_expectation
                            else ""
                        ),
                        now,
                        now,
                        now,
                        target_channel,
                    ),
                )
                notification = self._db.execute(
                    """SELECT * FROM notifications
                       WHERE id=? AND state='pending' AND claimed_at=?""",
                    (f"notification:{turn_id}", now),
                ).fetchone()
                if notification is not None:
                    self._queue_notification_row(notification, now, target_channel)
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   failure_reason=NULL, updated_at=? WHERE id=?""",
                (now, turn_id),
            )
        return len(messages) + len(progress_rows)

    @staticmethod
    def _reflection_slot(
        now: float, timezone: str, at: str
    ) -> tuple[str, float, datetime]:
        zone = ZoneInfo(timezone)
        local = datetime.fromtimestamp(now, zone)
        hour, minute = map(int, at.split(":"))
        scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scheduled.timestamp() > now:
            scheduled -= timedelta(days=1)
        local_date = (scheduled.date() - timedelta(days=1)).isoformat()
        return local_date, scheduled.timestamp(), scheduled

    def claim_manual_reflection(
        self,
        timezone: str,
        now: float | None = None,
    ) -> dict[str, object] | None:
        now = time.time() if now is None else now
        local_date = datetime.fromtimestamp(now, ZoneInfo(timezone)).date().isoformat()
        reflection_id = f"reflection:{local_date}"
        with self._db:
            self._db.execute(
                """INSERT OR IGNORE INTO reflections
                   (id, local_date, state, scheduled_at, created_at)
                   VALUES (?, ?, 'pending', ?, ?)""",
                (reflection_id, local_date, now, now),
            )
            row = self._db.execute(
                "SELECT * FROM reflections WHERE id=?",
                (reflection_id,),
            ).fetchone()
            if row is None or row["state"] == "running":
                return None
            self._db.execute(
                """UPDATE reflections SET state='running', claimed_at=?,
                   retry_at=NULL, error=NULL WHERE id=?""",
                (now, reflection_id),
            )
            claimed = self._db.execute(
                "SELECT * FROM reflections WHERE id=?",
                (reflection_id,),
            ).fetchone()
        return dict(claimed) if claimed is not None else None

    def claim_due_reflection(
        self,
        config: ReflectionConfig,
        timezone: str,
        now: float | None = None,
    ) -> dict[str, object] | None:
        if not config.enabled:
            return None
        now = time.time() if now is None else now
        local_date, scheduled_at, _ = self._reflection_slot(now, timezone, config.at)
        reflection_id = f"reflection:{local_date}"
        with self._db:
            self._db.execute(
                """INSERT OR IGNORE INTO reflections
                   (id, local_date, state, scheduled_at, created_at)
                   VALUES (?, ?, 'pending', ?, ?)""",
                (reflection_id, local_date, scheduled_at, now),
            )
            row = self._db.execute(
                """SELECT * FROM reflections
                   WHERE id=? AND state='pending' AND claimed_at IS NULL
                     AND scheduled_at<=? AND COALESCE(retry_at, 0)<=?""",
                (reflection_id, now, now),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                """UPDATE reflections SET state='running', claimed_at=?, error=NULL
                   WHERE id=?""",
                (now, reflection_id),
            )
        return dict(row)

    def next_reflection_due_at(
        self,
        config: ReflectionConfig,
        timezone: str,
        now: float | None = None,
    ) -> float | None:
        if not config.enabled:
            return None
        now = time.time() if now is None else now
        local_date, scheduled_at, scheduled = self._reflection_slot(
            now, timezone, config.at
        )
        row = self._db.execute(
            "SELECT state, retry_at FROM reflections WHERE local_date=?",
            (local_date,),
        ).fetchone()
        if row is None:
            return scheduled_at
        if row["state"] == "pending":
            return max(scheduled_at, float(row["retry_at"] or 0))
        if row["state"] == "running":
            return None
        next_scheduled = scheduled + timedelta(days=1)
        return next_scheduled.timestamp()

    def release_reflection(
        self, local_date: str, error: str, delay_seconds: float = 300
    ) -> None:
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE reflections SET state='pending', claimed_at=NULL,
                   retry_at=?, error=? WHERE local_date=? AND state='running'""",
                (now + delay_seconds, error[:500], local_date),
            )

    def restore_completed_reflection_claim(self, local_date: str) -> None:
        with self._db:
            self._db.execute(
                """UPDATE reflections SET state='completed', claimed_at=NULL,
                   retry_at=NULL, error=NULL
                   WHERE local_date=? AND state='running'""",
                (local_date,),
            )

    def reflection_source(
        self, local_date: str, timezone: str, token_budget: int
    ) -> dict[str, object]:
        zone = ZoneInfo(timezone)
        start = datetime.fromisoformat(f"{local_date}T00:00:00").replace(tzinfo=zone)
        end = start + timedelta(days=1)
        entries: list[tuple[float, str, str, bool, bool]] = []
        for row in self._db.execute(
            """SELECT role, content, created_at, delivery_state FROM messages
               WHERE created_at>=? AND created_at<?
                 AND (role IN ('user', 'event') OR delivery_state IN
                      ('delivered', 'uncertain', 'internal'))
               ORDER BY created_at""",
            (start.timestamp(), end.timestamp()),
        ).fetchall():
            owner = row["role"] == "user"
            if owner:
                label = "OWNER"
            elif row["role"] == "event":
                label = "EVENT"
            else:
                label = "MOMOI"
            if row["role"] != "event" and row["delivery_state"] == "internal":
                label = "MOMOI INTERNAL (not sent to owner)"
            elif row["role"] != "event" and row["delivery_state"] == "uncertain":
                label = "MOMOI DELIVERY UNCERTAIN"
            entries.append(
                (
                    float(row["created_at"]),
                    label,
                    str(row["content"]),
                    owner,
                    owner,
                )
            )
        for row in self._db.execute(
            """SELECT a.tool_name, a.state, a.ok, a.capability, t.started_at
               FROM tool_audit AS a JOIN turns AS t ON t.id=a.turn_id
               WHERE t.started_at>=? AND t.started_at<? ORDER BY t.started_at""",
            (start.timestamp(), end.timestamp()),
        ).fetchall():
            ok = "unknown" if row["ok"] is None else str(bool(row["ok"])).lower()
            entries.append(
                (
                    float(row["started_at"]),
                    f"TOOL {row['tool_name']}",
                    f"state={row['state']} ok={ok} capability={row['capability']}",
                    False,
                    False,
                )
            )
        for row in self._db.execute(
            """SELECT failure_reason, started_at FROM turns
               WHERE started_at>=? AND started_at<? AND failure_reason IS NOT NULL""",
            (start.timestamp(), end.timestamp()),
        ).fetchall():
            entries.append(
                (
                    float(row["started_at"]),
                    "RUNTIME FAILURE",
                    str(row["failure_reason"]),
                    False,
                    False,
                )
            )
        selected = _reflection_select_entries(entries, token_budget)
        text = "\n\n".join(
            f"[{context_timestamp(occurred_at)} {label}]\n{content}"
            for occurred_at, label, content, _, _ in selected
        )
        owner_text = "\n".join(content for _, _, content, owner, _ in selected if owner)
        knowledge_text = "\n".join(
            content for _, _, content, _, knowledge in selected if knowledge
        )

        mood_entries: list[str] = []
        mutation_entries: list[str] = []
        tool_calls: dict[tuple[str, str], dict[str, object]] = {}
        tool_order: list[tuple[str, str]] = []
        for row in self._db.execute(
            """SELECT j.turn_id, j.created_at, j.item_type, j.trust,
                      j.payload_json
               FROM turn_journal AS j JOIN turns AS t ON t.id=j.turn_id
               WHERE j.created_at>=? AND j.created_at<?
                 AND j.item_type IN ('final','tool_call','tool_result')
               ORDER BY j.created_at, j.sequence""",
            (start.timestamp(), end.timestamp()),
        ).fetchall():
            payload = _reflection_json(row["payload_json"], {})
            if not isinstance(payload, dict):
                continue
            stamp = context_timestamp(row["created_at"])
            if row["item_type"] in {"tool_call", "tool_result"}:
                call_id = str(payload.get("tool_call_id") or "unknown")
                identity = (str(row["turn_id"]), call_id)
                call = tool_calls.get(identity)
                if call is None:
                    call = {
                        "created_at": float(row["created_at"]),
                        "turn_id": str(row["turn_id"]),
                        "call_id": call_id,
                        "name": str(payload.get("name") or "unknown"),
                    }
                    tool_calls[identity] = call
                    tool_order.append(identity)
                if payload.get("name"):
                    call["name"] = str(payload["name"])
                if row["item_type"] == "tool_call":
                    call["source"] = str(payload.get("source") or "unknown")
                    call["arguments"] = payload.get("arguments", {})
                else:
                    call["result_trust"] = str(row["trust"])
                    call["ok"] = bool(payload.get("ok"))
                    if payload.get("error") not in (None, ""):
                        call["error"] = payload.get("error")
                    call["result"] = payload.get("result", {})
            elif row["item_type"] == "final":
                mood = payload.get("mood_change")
                if isinstance(mood, dict) and mood.get("state"):
                    mood_entries.append(
                        f"{stamp} state={mood.get('state')} "
                        f"intensity={mood.get('intensity', 'unknown')} "
                        f"cause={_reflection_compact_value(mood.get('cause'), 180)}"
                    )
                mutations = payload.get("mutations")
                if isinstance(mutations, dict):
                    for key, value in mutations.items():
                        if value in (None, [], {}, ""):
                            continue
                        if isinstance(value, list):
                            details = "; ".join(
                                _reflection_compact_value(item, 180) for item in value[:4]
                            )
                        else:
                            details = _reflection_compact_value(value, 300)
                        mutation_entries.append(f"{stamp} {key}: {details}")

        tool_entries: list[tuple[float, str, str, bool, bool]] = []
        for identity in tool_order:
            call = tool_calls[identity]
            details = [
                f"turn={call['turn_id']}",
                f"call={call['call_id']}",
                f"name={call['name']}",
                f"source={call.get('source', 'unknown')}",
                "arguments="
                + _reflection_compact_value(call.get("arguments", {}), 1200),
            ]
            if "ok" in call:
                details.append(f"ok={str(bool(call['ok'])).lower()}")
                if call.get("error") not in (None, ""):
                    details.append(
                        "error=" + _reflection_compact_value(call["error"], 400)
                    )
                details.append(
                    "result="
                    + _reflection_compact_value(call.get("result", {}), 1800)
                )
                details.append(
                    f"result_trust={call.get('result_trust', 'untrusted_tool_data')}"
                )
            else:
                details.append("result=(missing)")
            tool_entries.append(
                (
                    float(call["created_at"]),
                    f"TOOL TRACE {call['name']}",
                    " ".join(details),
                    False,
                    False,
                )
            )
        selected_tools = _reflection_select_entries(
            tool_entries,
            max(1000, min(8000, token_budget // 3)),
        )
        tool_timeline = "\n\n".join(
            f"[{context_timestamp(occurred_at)} {label}]\n{content}"
            for occurred_at, label, content, _, _ in selected_tools
        )

        topic_entries: list[str] = []
        episode_rows = self._db.execute(
            """SELECT title, status, working_summary, narrative_summary,
                      emotional_context_json, outcomes_json, topics_json,
                      open_loops_json, created_at, updated_at
               FROM conversation_episodes
               WHERE (created_at>=? AND created_at<?)
                  OR (updated_at>=? AND updated_at<?)
               ORDER BY updated_at""",
            (
                start.timestamp(),
                end.timestamp(),
                start.timestamp(),
                end.timestamp(),
            ),
        ).fetchall()
        for row in episode_rows:
            summary = str(row["narrative_summary"] or row["working_summary"] or "").strip()
            topics = _reflection_json(row["topics_json"], [])
            loops = _reflection_json(row["open_loops_json"], [])
            emotional = _reflection_json(row["emotional_context_json"], {})
            outcomes = _reflection_json(row["outcomes_json"], [])
            parts = [
                f"{context_timestamp(row['updated_at'])} {row['status']} {row['title']}",
            ]
            if summary:
                parts.append(f"summary={_reflection_compact_value(summary, 320)}")
            if topics:
                parts.append(f"topics={_reflection_compact_value(topics, 180)}")
            if emotional:
                parts.append(f"emotional_context={_reflection_compact_value(emotional, 180)}")
            if outcomes:
                parts.append(f"outcomes={_reflection_compact_value(outcomes, 180)}")
            if loops:
                parts.append(f"open_loops={_reflection_compact_value(loops, 180)}")
            topic_entries.append("; ".join(parts))
            if len(topic_entries) >= 16:
                break
        return {
            "text": text,
            "owner_text": owner_text,
            "knowledge_text": knowledge_text,
            "entries": len(selected),
            "mood_timeline": truncate_tokens(
                "\n".join(mood_entries), 1200
            ) or "(no recorded mood changes)",
            "topic_timeline": truncate_tokens(
                "\n".join(topic_entries), 2600
            ) or "(no topic episode changed)",
            "mutation_timeline": truncate_tokens(
                "\n".join(mutation_entries), 2600
            ) or "(no recorded state mutations)",
            "tool_timeline": tool_timeline or "(no journaled tool calls)",
            "start_at": start.timestamp(),
            "end_at": end.timestamp(),
        }

    def commit_reflection(
        self,
        local_date: str,
        turn_id: str,
        summary: str,
        memories: list[dict[str, object]],
        conversation_actions: list[dict[str, object]] | None = None,
        maintenance_turn_id: str = "",
    ) -> None:
        reflection_id = f"reflection:{local_date}"
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE reflections SET state='completed', claimed_at=NULL,
                   retry_at=NULL, summary=?, memories_json=?, error=NULL,
                   completed_at=? WHERE id=? AND state='running'""",
                (
                    summary,
                    json.dumps(memories, ensure_ascii=False, separators=(",", ":")),
                    now,
                    reflection_id,
                ),
            )
            self._db.execute(
                "DELETE FROM reflection_memories WHERE source_reflection_id=?",
                (reflection_id,),
            )
            for memory in memories:
                self._db.execute(
                    """INSERT INTO reflection_memories
                       (kind, key, content, evidence, confidence,
                        source_reflection_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(kind, key) DO UPDATE SET
                         content=excluded.content,
                         evidence=excluded.evidence,
                         confidence=excluded.confidence,
                         source_reflection_id=excluded.source_reflection_id,
                         updated_at=excluded.updated_at""",
                    (
                        memory["kind"],
                        memory["key"],
                        memory["content"],
                        memory["evidence"],
                        memory["confidence"],
                        reflection_id,
                        now,
                        now,
                    ),
                )
            self.apply_conversation_actions(conversation_actions or [], now=now)
            if maintenance_turn_id:
                self._db.execute(
                    """INSERT OR IGNORE INTO turns
                       (id, kind, source_ids_json, state, stage,
                        started_at, updated_at)
                       VALUES (?, 'autonomous', ?, 'running',
                               'memory_maintenance_queued', ?, ?)""",
                    (
                        maintenance_turn_id,
                        json.dumps([reflection_id]),
                        now,
                        now,
                    ),
                )
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   failure_reason=NULL, updated_at=? WHERE id=?""",
                (now, turn_id),
            )

    def reflection(self, local_date: str) -> dict[str, object] | None:
        row = self._db.execute(
            "SELECT * FROM reflections WHERE local_date=?", (local_date,)
        ).fetchone()
        return dict(row) if row else None

    def list_reflections(
        self, limit: int = 14, *, before: str | None = None
    ) -> dict[str, object]:
        if limit <= 0:
            return {"items": []}
        size = min(366, max(1, int(limit)))
        query = "SELECT * FROM reflections"
        params: list[object] = []
        cursor = str(before or "").strip()
        if cursor:
            query += " WHERE local_date < ?"
            params.append(cursor)
        query += " ORDER BY local_date DESC LIMIT ?"
        params.append(size + 1)
        rows = self._db.execute(query, params).fetchall()
        extra = len(rows) > size
        results: list[dict[str, object]] = []
        for row in rows[:size]:
            item = dict(row)
            try:
                memories = json.loads(str(item.pop("memories_json", "[]")))
            except (json.JSONDecodeError, TypeError):
                memories = []
            item["memories"] = memories if isinstance(memories, list) else []
            _add_context_timestamps(
                item, ("scheduled_at", "retry_at", "created_at", "completed_at")
            )
            results.append(item)
        payload: dict[str, object] = {"items": results}
        if extra and results:
            payload["next_cursor"] = results[-1]["local_date"]
        return payload

    def dashboard_overview(self) -> dict[str, object]:
        counts = {
            "conversations": int(
                self._db.execute(
                    "SELECT COUNT(*) FROM conversation_episodes"
                ).fetchone()[0]
            ),
            "messages": int(
                self._db.execute(
                    """SELECT COUNT(*) FROM messages
                       WHERE role IN ('user', 'event') OR delivery_state IN
                           ('delivered', 'uncertain', 'internal')"""
                ).fetchone()[0]
            ),
            "reflections": int(
                self._db.execute(
                    "SELECT COUNT(*) FROM reflections WHERE state='completed'"
                ).fetchone()[0]
            ),
            "goals": int(
                self._db.execute(
                    """SELECT COUNT(*) FROM goals
                       WHERE status IN ('active', 'waiting', 'blocked')"""
                ).fetchone()[0]
            ),
            "emotions": int(
                self._db.execute("SELECT COUNT(*) FROM emotions").fetchone()[0]
            ),
            "memories": int(
                self._db.execute(
                    """SELECT COUNT(*) FROM memories AS m
                       WHERE m.superseded_by IS NULL
                         AND (m.expires_at IS NULL OR m.expires_at > ?)
                         AND NOT EXISTS (
                             SELECT 1 FROM memory_tombstones AS t
                             WHERE t.kind=m.kind AND t.key=m.key
                         )""",
                    (time.time(),),
                ).fetchone()[0]
            ),
        }
        latest_message = self._db.execute(
            """SELECT MAX(created_at) FROM messages
               WHERE role IN ('user', 'event') OR delivery_state IN
                   ('delivered', 'uncertain', 'internal')"""
        ).fetchone()[0]
        state = self.self_state()
        waiting = bool(str(state.get("pending_reply_expectation") or "").strip())
        return {
            "counts": counts,
            "mood": {
                "state": state["mood_state"],
                "intensity": state["mood_intensity"],
                "cause": state["mood_cause"],
                "updated_at": _dashboard_unix(state["mood_updated_at"]),
            },
            "activity": {
                "name": state["activity"],
                "result": state.get("activity_result") or "",
                "since": state["activity_since"],
                "since_timestamp": context_timestamp(state["activity_since"]),
            },
            "heartbeat": {
                "next_at": _dashboard_unix(state.get("next_heartbeat_at")),
                "last_at": _dashboard_unix(state.get("last_heartbeat_at")),
                "running": state.get("heartbeat_claimed_at") is not None,
                "kind": state.get("heartbeat_claim_kind"),
                "reply_check_at": (
                    _dashboard_unix(state.get("pending_reply_next_check_at"))
                    if waiting
                    else None
                ),
            },
            "latest_message_at": latest_message,
            "latest_message_timestamp": (
                context_timestamp(latest_message) if latest_message is not None else None
            ),
            "usage": self.dashboard_usage(days=30),
        }

    def list_memories(self, limit: int = 200) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        self.purge_expired_memories()
        now = time.time()
        rows = self._db.execute(
            """SELECT id, kind, key, content, activation, authority,
                      evidence_quote, importance, created_at, updated_at,
                      expires_at
               FROM memories AS m
               WHERE m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND (m.activation<>'recent' OR m.updated_at>=?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )
               ORDER BY CASE m.activation
                          WHEN 'always' THEN 0
                          WHEN 'recent' THEN 1
                          ELSE 2
                        END,
                        m.updated_at DESC, m.id DESC
               LIMIT ?""",
            (now, now - RECENT_MEMORY_WINDOW_SECONDS, limit),
        ).fetchall()
        results: list[dict[str, object]] = []
        for row in rows:
            results.append(self._memory_public_dict(row))
        return results

    def _memory_public_dict(self, row: sqlite3.Row) -> dict[str, object]:
        item = dict(row)
        item["evidence"] = item.pop("evidence_quote")
        _add_context_timestamps(item, ("created_at", "updated_at", "expires_at"))
        return item

    def _active_memory_row(self, memory_id: int) -> sqlite3.Row | None:
        return self._db.execute(
            """SELECT id, kind, key, content, activation, authority,
                      evidence_quote, importance, created_at, updated_at,
                      expires_at
               FROM memories AS m
               WHERE m.id=?
                 AND m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )""",
            (memory_id, time.time()),
        ).fetchone()

    def update_memory_content(
        self, memory_id: int, content: str
    ) -> dict[str, object] | None:
        text = content.strip()
        if not text or len(text) > 2000:
            raise ValueError("content must contain between 1 and 2000 characters")
        now = time.time()
        with self._db:
            row = self._active_memory_row(memory_id)
            if row is None:
                return None
            self._db.execute(
                "UPDATE memories SET content=?, updated_at=? WHERE id=?",
                (text, now, memory_id),
            )
        updated = self._active_memory_row(memory_id)
        return self._memory_public_dict(updated) if updated else None

    def forget_memory_by_id(self, memory_id: int, reason: str) -> bool:
        text = reason.strip() or "Deleted from dashboard"
        if len(text) > 500:
            raise ValueError("reason must contain at most 500 characters")
        now = time.time()
        with self._db:
            row = self._active_memory_row(memory_id)
            if row is None:
                return False
            self._db.execute(
                """INSERT INTO memory_tombstones
                   (kind, key, source_event_id, evidence_quote, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(kind, key) DO UPDATE SET
                     source_event_id=excluded.source_event_id,
                     evidence_quote=excluded.evidence_quote,
                     created_at=excluded.created_at""",
                (
                    row["kind"],
                    row["key"],
                    "dashboard:forget",
                    text,
                    now,
                ),
            )
        return True

    def goal(self, goal_id: str) -> dict[str, object] | None:
        row = self._db.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        return self._goal_dict(row) if row else None

    def list_goals(self, include_closed: bool = False) -> list[dict[str, object]]:
        where = (
            "" if include_closed else "WHERE status IN ('active', 'waiting', 'blocked')"
        )
        rows = self._db.execute(
            f"SELECT * FROM goals {where} ORDER BY updated_at DESC"
        ).fetchall()
        return [self._goal_dict(row) for row in rows]

    def update_goal_owner(
        self,
        goal_id: str,
        *,
        title: str | None = None,
        success_criteria: str | None = None,
        next_action: str | None = None,
        status: str | None = None,
        waiting_for: str | None = None,
        blocked_reason: str | None = None,
    ) -> dict[str, object] | None:
        goal = self.goal(goal_id)
        if goal is None:
            return None
        if goal["status"] in {"done", "cancelled"}:
            raise ValueError("closed goal cannot be updated")
        if title is not None:
            text = title.strip()
            if not text:
                raise ValueError("title must not be empty")
            goal["title"] = text[:500]
        if success_criteria is not None:
            text = success_criteria.strip()
            if not text:
                raise ValueError("success_criteria must not be empty")
            goal["success_criteria"] = text[:2000]
        if next_action is not None:
            goal["next_action"] = next_action.strip()[:2000]
        if waiting_for is not None:
            goal["waiting_for"] = waiting_for.strip()[:2000]
        if blocked_reason is not None:
            goal["blocked_reason"] = blocked_reason.strip()[:2000]
        if status is not None:
            if status not in {"active", "waiting", "blocked"}:
                raise ValueError("status must be active, waiting, or blocked")
            goal["status"] = status
        if goal["status"] == "active" and not goal.get("next_action"):
            raise ValueError("active goal requires next_action")
        if goal["status"] == "waiting" and not goal.get("waiting_for"):
            raise ValueError("waiting goal requires waiting_for")
        if goal["status"] == "blocked" and not goal.get("blocked_reason"):
            raise ValueError("blocked goal requires blocked_reason")
        if goal["status"] == "blocked":
            goal["next_review_at"] = None
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE goals
                   SET title=?, success_criteria=?, status=?, next_action=?,
                       waiting_for=?, blocked_reason=?,
                       next_review_at=?, review_claimed_at=NULL, updated_at=?
                   WHERE id=?""",
                (
                    goal["title"],
                    goal["success_criteria"],
                    goal["status"],
                    goal.get("next_action", ""),
                    goal.get("waiting_for", ""),
                    goal.get("blocked_reason", ""),
                    goal.get("next_review_at"),
                    now,
                    goal_id,
                ),
            )
        return self.goal(goal_id)

    def cancel_goal(self, goal_id: str, reason: str) -> dict[str, object] | None:
        text = reason.strip()
        if not text:
            raise ValueError("reason is required")
        goal = self.goal(goal_id)
        if goal is None:
            return None
        if goal["status"] in {"done", "cancelled"}:
            raise ValueError("closed goal cannot be cancelled")
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE goals
                   SET status='cancelled', latest_result=?, next_review_at=NULL,
                       review_claimed_at=NULL, updated_at=?
                   WHERE id=?""",
                (text[:2000], now, goal_id),
            )
        return self.goal(goal_id)

    def commit_goal_draft(self, draft: TurnDraft) -> None:
        with self._db:
            self._apply_goal_mutations(draft, time.time())

    def active_goals_context(self, authority: str | None = None) -> str:
        authority_clause = " AND authority=?" if authority else ""
        rows = self._db.execute(
            f"""SELECT * FROM goals
               WHERE status IN ('active', 'waiting', 'blocked')
               {authority_clause}
               ORDER BY COALESCE(next_review_at, 1e30), updated_at DESC
               LIMIT 20""",
            (authority,) if authority else (),
        ).fetchall()
        if not rows:
            return ""
        lines = []
        for row in rows:
            goal = self._goal_dict(row)
            lines.append(
                f"- id={goal['id']} status={goal['status']} title={goal['title']} "
                f"next_action={goal['next_action'] or 'none'} "
                f"next_review_at={goal.get('next_review_timestamp') or 'none'} "
                f"retry_at={goal.get('retry_timestamp') or 'none'} "
                f"schedule={json.dumps(goal['schedule'], ensure_ascii=False) if goal['schedule'] else 'none'}"
            )
        return "\n".join(lines)

    def add_emotion(
        self, slug: str, path: str | Path, description: str
    ) -> dict[str, object]:
        slug = slug.strip()
        description = description.strip()
        asset = self._resolve_asset_path(path)
        if not valid_emotion_slug(slug):
            raise ValueError(
                "slug must use lowercase letters, digits, dot, underscore, or hyphen"
            )
        if not asset.is_file():
            raise ValueError("path must be an existing file")
        if not description or len(description) > 500:
            raise ValueError("description must contain 1 to 500 characters")
        now = time.time()
        with self._db:
            self._db.execute(
                """INSERT INTO emotions(slug, path, description, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET path=excluded.path,
                     description=excluded.description, updated_at=excluded.updated_at""",
                (slug, self._stored_asset_path(asset), description, now, now),
            )
        return self.emotion(slug) or {}

    def delete_emotion(self, slug: str) -> bool:
        with self._db:
            cursor = self._db.execute("DELETE FROM emotions WHERE slug=?", (slug,))
        return cursor.rowcount == 1

    def emotion(self, slug: str) -> dict[str, object] | None:
        row = self._db.execute(
            "SELECT id, slug, path, description FROM emotions WHERE slug=?", (slug,)
        ).fetchone()
        return self._emotion_dict(row) if row else None

    def list_emotions(self) -> list[dict[str, object]]:
        rows = self._db.execute(
            "SELECT id, slug, path, description FROM emotions ORDER BY id"
        ).fetchall()
        return [self._emotion_dict(row) for row in rows]

    def _emotion_dict(self, row: sqlite3.Row) -> dict[str, object]:
        item = dict(row)
        item["path"] = str(self._resolve_asset_path(str(item["path"])))
        return item

    def _resolve_asset_path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self._workspace / path).resolve()

    def _stored_asset_path(self, value: str | Path) -> str:
        path = self._resolve_asset_path(value)
        try:
            return path.relative_to(self._workspace).as_posix()
        except ValueError:
            return str(path)

    def _recover_emotion_outbox(self) -> None:
        with self._db:
            failed = self._db.execute(
                """SELECT id, media_path FROM outbox
                   WHERE state='failed' AND kind='image'
                     AND last_error LIKE 'media asset cannot be read:%'"""
            ).fetchall()
            for message in failed:
                if (
                    message["media_path"]
                    and self._resolve_asset_path(str(message["media_path"])).is_file()
                ):
                    self._db.execute(
                        """UPDATE outbox SET state='pending', attempts=0,
                           last_error=NULL, next_attempt_at=0 WHERE id=?""",
                        (message["id"],),
                    )
                    self._sync_outbox_message(int(message["id"]), "pending")

    def emotion_path_referenced(
        self, path: str, *, exclude_slug: str | None = None
    ) -> bool:
        path = self._stored_asset_path(path)
        return (
            self._db.execute(
                """SELECT 1 FROM emotions
               WHERE path=? AND (? IS NULL OR slug<>?)
               UNION ALL
               SELECT 1 FROM outbox
               WHERE media_path=? AND state NOT IN ('sent', 'failed', 'superseded')
               UNION ALL
               SELECT 1 FROM notifications AS n
               JOIN emotions AS e ON e.path=?
               WHERE n.state='pending'
                 AND instr(n.messages_json, 'emotion://' || e.slug) > 0
               LIMIT 1""",
                (path, exclude_slug, exclude_slug, path, path),
            ).fetchone()
            is not None
        )

    def emotion_context(self, token_budget: int = 4000) -> str:
        lines: list[str] = []
        tokens = 0
        for row in self.list_emotions():
            line = f"- slug={row['slug']} meaning={row['description']}"
            line_tokens = estimate_tokens(line)
            if tokens + line_tokens > token_budget:
                break
            lines.append(line)
            tokens += line_tokens
        return "\n".join(lines)

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

    @staticmethod
    def _goal_dict(row: sqlite3.Row) -> dict[str, object]:
        goal = dict(row)
        _add_context_timestamps(
            goal,
            ("next_review_at", "retry_at", "created_at", "updated_at"),
        )
        goal["plan"] = json.loads(str(goal.pop("plan_json")))
        schedule_json = str(goal.pop("schedule_json", ""))
        goal["schedule"] = json.loads(schedule_json) if schedule_json else None
        return goal

    def claim_due_goal(self) -> dict[str, object] | None:
        now = time.time()
        with self._db:
            row = self._db.execute(
                """SELECT * FROM goals
                   WHERE status IN ('active', 'waiting')
                     AND COALESCE(retry_at, next_review_at) <= ?
                     AND review_claimed_at IS NULL
                   ORDER BY COALESCE(retry_at, next_review_at) LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                "UPDATE goals SET review_claimed_at=? WHERE id=?",
                (now, row["id"]),
            )
        return self._goal_dict(row)

    def next_goal_due_at(self) -> float | None:
        row = self._db.execute(
            """SELECT MIN(COALESCE(retry_at, next_review_at)) AS due FROM goals
               WHERE status IN ('active', 'waiting') AND review_claimed_at IS NULL"""
        ).fetchone()
        return float(row["due"]) if row and row["due"] is not None else None

    def release_goal_claim(self, goal_id: str, *, defer_seconds: float = 0) -> None:
        with self._db:
            if defer_seconds:
                self._db.execute(
                    """UPDATE goals SET review_claimed_at=NULL, next_review_at=?,
                       retry_at=NULL, failure_count=0, updated_at=?
                       WHERE id=? AND status IN ('active', 'waiting')""",
                    (time.time() + defer_seconds, time.time(), goal_id),
                )
            else:
                self._db.execute(
                    "UPDATE goals SET review_claimed_at=NULL WHERE id=?", (goal_id,)
                )

    def defer_goal_failure(self, goal_id: str) -> float | None:
        row = self._db.execute(
            "SELECT failure_count FROM goals WHERE id=?", (goal_id,)
        ).fetchone()
        if row is None:
            return None
        count = int(row["failure_count"]) + 1
        delays = (300, 900, 3600, 10800, 21600)
        retry_at = time.time() + delays[min(count - 1, len(delays) - 1)]
        with self._db:
            self._db.execute(
                """UPDATE goals SET failure_count=?, retry_at=?,
                   review_claimed_at=NULL, updated_at=? WHERE id=?""",
                (count, retry_at, time.time(), goal_id),
            )
        return retry_at

    def append_turn_journal(
        self,
        turn_id: str,
        item_type: str,
        payload: dict[str, object],
        *,
        visibility: str = "internal",
        trust: str = "runtime",
        created_at: float | None = None,
    ) -> int:
        if visibility not in {"owner", "internal"}:
            raise ValueError("invalid journal visibility")
        if trust not in {
            "owner",
            "runtime",
            "context_data",
            "untrusted_tool_data",
        }:
            raise ValueError("invalid journal trust")
        now = time.time() if created_at is None else float(created_at)
        with self._db:
            return self._append_turn_journal(
                turn_id,
                item_type,
                payload,
                visibility=visibility,
                trust=trust,
                created_at=now,
            )

    def _append_turn_journal(
        self,
        turn_id: str,
        item_type: str,
        payload: dict[str, object],
        *,
        visibility: str,
        trust: str,
        created_at: float,
    ) -> int:
        sequence = int(
            self._db.execute(
                """SELECT COALESCE(MAX(sequence), 0) + 1
                   FROM turn_journal WHERE turn_id=?""",
                (turn_id,),
            ).fetchone()[0]
        )
        self._db.execute(
            """INSERT INTO turn_journal
               (turn_id, sequence, created_at, item_type, visibility, trust,
                payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                turn_id,
                sequence,
                created_at,
                str(item_type),
                visibility,
                trust,
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ),
            ),
        )
        return sequence

    def begin_tool_call(
        self,
        turn_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        capability: str = "external_effect",
    ) -> dict[str, object] | None:
        if capability not in {"read", "write", "external_effect"}:
            raise ValueError("invalid tool capability")
        serialized = json.dumps(
            arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        row = self._db.execute(
            """SELECT tool_name, arguments_sha256, state, result_json
               FROM tool_audit WHERE turn_id=? AND tool_call_id=?""",
            (turn_id, tool_call_id),
        ).fetchone()
        if row is not None:
            if row["tool_name"] != tool_name or row["arguments_sha256"] != digest:
                return {"ok": False, "error": "tool_call_id_conflict"}
            if row["state"] == "completed" and row["result_json"] is not None:
                return json.loads(str(row["result_json"]))
            return {
                "ok": False,
                "error": "previous_call_incomplete",
                "ambiguous": True,
            }
        with self._db:
            if capability != "read":
                self._db.execute(
                    """UPDATE turns SET external_effect_started=1,
                       stage='tool_dispatch', updated_at=?
                       WHERE id=? AND state='running'""",
                    (time.time(), turn_id),
                )
            else:
                self._db.execute(
                    """UPDATE turns SET stage='tool_dispatch', updated_at=?
                       WHERE id=? AND state='running'""",
                    (time.time(), turn_id),
                )
            self._db.execute(
                """INSERT INTO tool_audit
                   (turn_id, tool_call_id, tool_name, capability, arguments_sha256,
                    state, started_at)
                   VALUES (?, ?, ?, ?, ?, 'dispatching', ?)""",
                (turn_id, tool_call_id, tool_name, capability, digest, time.time()),
            )
        return None

    def complete_tool_call(
        self, turn_id: str, tool_call_id: str, result: dict[str, object]
    ) -> None:
        with self._db:
            self._db.execute(
                """UPDATE tool_audit
                   SET state='completed', result_json=?, ok=?, completed_at=?
                   WHERE turn_id=? AND tool_call_id=?""",
                (
                    json.dumps(result, ensure_ascii=False),
                    int(bool(result.get("ok"))),
                    time.time(),
                    turn_id,
                    tool_call_id,
                ),
            )
            self._db.execute(
                """UPDATE turns SET stage='tool_completed', updated_at=?
                   WHERE id=? AND state='running'""",
                (time.time(), turn_id),
            )

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
                   (id, kind, source_ids_json, state, started_at, updated_at)
                   VALUES (?, 'owner', ?, 'running', ?, ?)""",
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
                    next_schedule_at(current["schedule"], now)
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
                self._ensure_autonomous_episode(
                    f"goal:{goal_id}",
                    turn_id,
                    str(current["title"]),
                    now,
                    goal_record,
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
            eligible = max(eligible, quiet_until(now, config))
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
            episode_key, title = (
                self._heartbeat_day_episode(
                    self._heartbeat_turn_time(str(row["turn_id"]), now)
                )
                if row["goal_id"] == "heartbeat"
                else (
                    f"goal:{row['goal_id']}",
                    self._episode_title(
                        visible_messages[0], "Autonomous conversation"
                    ),
                )
            )
            self._ensure_autonomous_episode(
                episode_key,
                str(row["turn_id"]),
                title,
                now,
                visible_messages,
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

    def _apply_goal_mutations(self, draft: TurnDraft | None, now: float) -> None:
        for goal in draft.goals.values() if draft else []:
            self._db.execute(
                """INSERT INTO goals
                   (id, title, success_criteria, authority, source_event_id, status,
                    plan_json, next_action, waiting_for, blocked_reason, latest_result, schedule_json,
                    next_review_at, review_claimed_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     title=excluded.title,
                     success_criteria=excluded.success_criteria,
                     status=excluded.status,
                     plan_json=excluded.plan_json,
                     next_action=excluded.next_action,
                     waiting_for=excluded.waiting_for,
                     blocked_reason=excluded.blocked_reason,
                     latest_result=excluded.latest_result,
                     schedule_json=excluded.schedule_json,
                     next_review_at=excluded.next_review_at,
                     retry_at=NULL,
                     failure_count=0,
                     review_claimed_at=NULL,
                     updated_at=excluded.updated_at""",
                (
                    goal["id"],
                    goal["title"],
                    goal["success_criteria"],
                    goal["authority"],
                    goal["source_event_id"],
                    goal["status"],
                    json.dumps(goal.get("plan", []), ensure_ascii=False),
                    goal.get("next_action", ""),
                    goal.get("waiting_for", ""),
                    goal.get("blocked_reason", ""),
                    goal.get("latest_result", ""),
                    json.dumps(goal.get("schedule"), ensure_ascii=False)
                    if goal.get("schedule")
                    else "",
                    goal.get("next_review_at"),
                    now,
                    now,
                ),
            )
