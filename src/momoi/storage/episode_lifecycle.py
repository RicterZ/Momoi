from __future__ import annotations

import logging
import uuid

from ..observability.events import log_event
from .memory_values import estimate_tokens

logger = logging.getLogger(__name__)


class EpisodeLifecycleStore:
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
