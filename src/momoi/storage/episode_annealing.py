import json
import logging
import math
import time

from ..observability.events import log_event
from .memory_values import estimate_tokens

EPISODE_ANNEAL_MAX_FAILURES = 3
logger = logging.getLogger(__name__)


class EpisodeAnnealingStore:
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

