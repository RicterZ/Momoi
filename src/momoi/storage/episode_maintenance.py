import json
import logging
import math
import re
import sqlite3
import time
import uuid

from ..logging_context import log_event
from .episode_sql import runtime_archive_kind_sql
from .memory_values import estimate_tokens

EPISODE_ANNEAL_MAX_FAILURES = 3
EPISODE_CONSOLIDATION_LOOKBACK_SECONDS = 30 * 24 * 60 * 60
logger = logging.getLogger(__name__)


class EpisodeMaintenanceStore:
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
            item["timestamp"] = self.context_timestamp(item["created_at"])
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
            f"""SELECT t.id, t.updated_at, et.episode_id
               FROM turns AS t
               JOIN episode_turns AS et ON et.turn_id=t.id
               JOIN conversation_episodes AS e ON e.id=et.episode_id
               WHERE t.kind='owner' AND t.state='completed'
                 AND t.updated_at>?
                 AND {runtime_archive_kind_sql('e')} IS NULL
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
                    "timestamp": self.context_timestamp(row["updated_at"]),
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
                    "timestamp": self.context_timestamp(row["updated_at"]),
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
                        item["timestamp"] = self.context_timestamp(item["created_at"])
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
