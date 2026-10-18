from __future__ import annotations

import json
import sqlite3
import time
import uuid

from .episode_cues import cue_texts
from ...memory.metadata import validate_episode_triggers, normalize_trigger
from ..core.integrity import decode_stored_json
from ..core.timestamps import add_context_timestamps

EPISODE_IDLE_CLOSE_SECONDS = 4 * 60 * 60


class EpisodeRecordStore:
    def close_idle_episodes(self, *, now: float | None = None) -> int:
        """Close topics after four hours without a linked conversation message."""
        now = time.time() if now is None else now
        with self._db:
            cursor = self._db.execute(
                """UPDATE conversation_episodes
                   SET status='closed', closed_at=?
                   WHERE status IN ('open', 'closing') AND archive_kind IS NULL
                     AND COALESCE((
                         SELECT MAX(m.created_at) FROM episode_turns et
                         JOIN messages m ON m.turn_id=et.turn_id
                         WHERE et.episode_id=conversation_episodes.id
                     ), created_at)<=?""",
                (now, now - EPISODE_IDLE_CLOSE_SECONDS),
            )
        return cursor.rowcount

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
        episode["recall_cues"] = decode_stored_json(
            episode.pop("recall_cues_json"),
            entity="conversation_episode",
            record_id=episode_id,
            field="recall_cues_json",
            expected_type=list,
            fallback=[],
        )
        episode["recall_cue_sources"] = [
            cue for cue in episode["recall_cues"] if isinstance(cue, dict)
        ]
        episode["recall_cues"] = cue_texts(episode["recall_cues"])
        episode.pop("summary", None)
        for name in ("topics", "entities", "open_loops", "triggers"):
            episode[name] = json.loads(str(episode.pop(f"{name}_json")))
        return episode

    def set_episode_triggers(self, episode_id: str, triggers: list[str]) -> dict[str, object]:
        triggers = validate_episode_triggers(triggers)
        with self._db:
            cursor = self._db.execute(
                "UPDATE conversation_episodes SET triggers_json=?, triggers_manual=1 WHERE id=?",
                (json.dumps(triggers, ensure_ascii=False), episode_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(episode_id)
        return self.episode(episode_id)

    def triggered_episodes(self, text: str, *, limit: int = 5) -> list[dict[str, str]]:
        """Literal topic lookup; return only titles and IDs, never full archives."""
        if not isinstance(text, str):
            raise ValueError("trigger input must be a string")
        if not text.strip() or limit <= 0:
            return []
        normalized_text = normalize_trigger(text)
        matches = []
        for row in self._db.execute(
            "SELECT id,title,triggers_json,updated_at FROM conversation_episodes "
            "WHERE triggers_json!='[]' AND archive_kind IS NULL"
        ):
            words = [normalize_trigger(word) for word in json.loads(row["triggers_json"]) if word]
            lengths = [len(word) for word in words if word in normalized_text]
            if lengths:
                matches.append((max(lengths), row["updated_at"], row["id"], row["title"]))
        matches.sort(reverse=True)
        return [{"id": item[2], "title": item[3]} for item in matches[:min(limit, 5)]]

    def create_episode(
        self,
        title: str,
        *,
        episode_id: str | None = None,
        topics: list[object] | None = None,
        entities: list[object] | None = None,
        open_loops: list[object] | None = None,
    ) -> dict[str, object]:
        title = title.strip()
        if not title:
            raise ValueError("episode title is required")
        episode_id = episode_id or uuid.uuid4().hex
        now = time.time()
        with self._db:
            self._db.execute(
                """INSERT INTO conversation_episodes
                   (id, title, topics_json, entities_json, open_loops_json,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
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
