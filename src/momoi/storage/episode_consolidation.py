import json
import re
import sqlite3
import time
import uuid

from .episode_sql import runtime_archive_kind_sql

EPISODE_CONSOLIDATION_LOOKBACK_SECONDS = 30 * 24 * 60 * 60
EPISODE_CONSOLIDATION_BATCH_SIZE = 12
EPISODE_CONSOLIDATION_DEFER_TIMEOUT_SECONDS = 8 * 60 * 60
EPISODE_CONSOLIDATION_DEFER_TIMEOUT_REASON = "defer_timeout_8h"


class EpisodeConsolidationStore:
    def cleanup_expired_episode_consolidation_deferrals(
        self, *, now: float | None = None
    ) -> int:
        """Ignore deferred Turns after their bounded context-wait window."""

        now = time.time() if now is None else now
        cutoff = now - EPISODE_CONSOLIDATION_DEFER_TIMEOUT_SECONDS
        with self._db:
            cursor = self._db.execute(
                """UPDATE episode_consolidation_decisions
                   SET action='ignored', episode_id=NULL, reason=?, processed_at=?
                   WHERE action='deferred' AND processed_at<=?""",
                (
                    EPISODE_CONSOLIDATION_DEFER_TIMEOUT_REASON,
                    now,
                    cutoff,
                ),
            )
        return max(0, cursor.rowcount)

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

    def episode_consolidation_pending_count(
        self, limit: int = EPISODE_CONSOLIDATION_BATCH_SIZE
    ) -> int:
        return len(self._episode_consolidation_pending_rows(limit))

    def claim_episode_consolidation_candidate(
        self,
        limit: int = EPISODE_CONSOLIDATION_BATCH_SIZE,
        *,
        minimum: int = EPISODE_CONSOLIDATION_BATCH_SIZE,
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
