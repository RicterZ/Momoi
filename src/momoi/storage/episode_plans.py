from __future__ import annotations

import json
import logging

from ..logging_context import log_event
from .context_plan_adapter import normalize_context_plan
from .episode_sql import runtime_archive_kind_sql

logger = logging.getLogger(__name__)


class EpisodePlanStore:
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
                episode_id = self._roll_episode(episode_id, turn_id, now, raw_text)
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
