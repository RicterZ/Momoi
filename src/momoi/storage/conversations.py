from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from .context_plan_adapter import normalize_context_plan
from .episode_sql import runtime_archive_kind_sql
from .integrity import decode_stored_json
from .memory import estimate_tokens
from .timestamps import add_context_timestamps
from ..logging_context import log_event

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)



class ConversationStore:
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
