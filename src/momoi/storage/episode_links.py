from __future__ import annotations

import json
import time


class EpisodeLinkStore:
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
        if (
            kind in {"continues", "supersedes"}
            and self._episode_ordering_link_creates_cycle(
                from_episode_id, to_episode_id
            )
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
