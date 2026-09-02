import json
import logging
import uuid
from datetime import datetime

from ..logging_context import log_event
from .episode_sql import runtime_archive_kind_sql

logger = logging.getLogger(__name__)


class RuntimeArchiveStore:
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

