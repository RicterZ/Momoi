"""Independent, evidence-backed relations between summarized Episodes."""

from __future__ import annotations

import json
import time

from .episode_sql import runtime_archive_kind_sql


class EpisodeRelationStore:
    def episode_relation_neighbors(
        self, episode_ids: list[str], *, per_episode: int = 5,
    ) -> dict[str, list[dict[str, str]]]:
        """Read current, positive neighbors for recalled Episodes in one query."""
        ids = list(dict.fromkeys(value for value in episode_ids if value))
        if not ids or per_episode <= 0:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self._db.execute(
            f"""SELECT r.source_episode_id, r.target_episode_id, r.relation,
                       r.explanation, r.source_summary_ordinal,
                       source.summarized_through_ordinal AS current_source_ordinal,
                       target.id AS target_id, target.title AS target_title,
                       target.narrative_summary AS target_summary,
                       source.id AS source_id, source.title AS source_title,
                       source.narrative_summary AS source_summary,
                       r.updated_at
                FROM episode_relations r
                JOIN conversation_episodes source ON source.id=r.source_episode_id
                JOIN conversation_episodes target ON target.id=r.target_episode_id
                WHERE r.source_episode_id IN ({placeholders})
                   OR r.target_episode_id IN ({placeholders})
                ORDER BY CASE r.relation
                             WHEN 'revises' THEN 0
                             WHEN 'follows_up' THEN 1
                             WHEN 'context' THEN 2
                             ELSE 3 END,
                         r.updated_at DESC, r.source_episode_id, r.target_episode_id""",
            (*ids, *ids),
        ).fetchall()
        result: dict[str, list[dict[str, str]]] = {value: [] for value in ids}
        for row in rows:
            if int(row["source_summary_ordinal"]) != int(row["current_source_ordinal"]):
                continue
            for selected_id, direction, prefix in (
                (row["source_episode_id"], "outgoing", "target"),
                (row["target_episode_id"], "incoming", "source"),
            ):
                if selected_id not in result or len(result[selected_id]) >= per_episode:
                    continue
                result[selected_id].append({
                    "direction": direction,
                    "type": str(row["relation"]),
                    "episode_id": str(row[f"{prefix}_id"]),
                    "title": str(row[f"{prefix}_title"]),
                    "summary": str(row[f"{prefix}_summary"] or "")[:240],
                    "explanation": str(row["explanation"]),
                })
        return result

    def episode_relation_graph(self, episode_id: str, depth: int = 1) -> dict[str, object]:
        """Expand both incoming and outgoing links without truncating the graph."""
        if depth not in (1, 2):
            raise ValueError("depth must be 1 or 2")
        root = self.episode(episode_id)
        if root is None:
            raise ValueError("episode not found")
        nodes = {episode_id: {
            "id": episode_id, "title": root["title"],
            "summary": root["narrative_summary"], "depth": 0,
        }}
        edges: dict[tuple[str, str], dict[str, str]] = {}
        frontier = [episode_id]
        for level in range(1, depth + 1):
            neighbors = self.episode_relation_neighbors(frontier, per_episode=1_000_000)
            next_frontier = []
            for current_id, related in neighbors.items():
                for item in related:
                    other_id = item["episode_id"]
                    source_id, target_id = (
                        (current_id, other_id) if item["direction"] == "outgoing"
                        else (other_id, current_id)
                    )
                    edges[source_id, target_id] = {
                        "source_episode_id": source_id,
                        "target_episode_id": target_id,
                        "type": item["type"],
                        "explanation": item["explanation"],
                    }
                    if other_id not in nodes:
                        other = self.episode(other_id)
                        nodes[other_id] = {
                            "id": other_id, "title": item["title"],
                            "summary": other["narrative_summary"] if other else item["summary"],
                            "depth": level,
                        }
                        next_frontier.append(other_id)
            frontier = next_frontier
            if not frontier:
                break
        return {"root_episode_id": episode_id, "depth": depth,
                "nodes": list(nodes.values()), "edges": list(edges.values())}

    def claim_episode_relation_candidate(self) -> dict[str, object] | None:
        now = time.time()
        with self._db:
            row = self._db.execute(
                f"""SELECT e.* FROM conversation_episodes e
                   LEFT JOIN episode_relation_jobs j ON j.episode_id=e.id
                   WHERE e.created_at >= (SELECT enabled_at FROM episode_relation_settings WHERE id=1)
                     AND COALESCE({runtime_archive_kind_sql('e')}, '') NOT IN ('webhook', 'heartbeat')
                     AND e.narrative_summary<>''
                     AND e.summarized_through_ordinal>COALESCE(j.processed_ordinal, 0)
                     AND COALESCE(j.retry_at, 0)<=?
                     AND j.claimed_at IS NULL
                   ORDER BY e.created_at LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                """INSERT INTO episode_relation_jobs(episode_id, claimed_at)
                   VALUES (?, ?) ON CONFLICT(episode_id) DO UPDATE SET claimed_at=excluded.claimed_at""",
                (row["id"], now),
            )
        return self._episode_dict(row)

    def episode_relation_target_eligible(self, source, target) -> bool:
        return (str(target["id"]) != str(source["id"])
                and float(target["created_at"]) < float(source["created_at"])
                and self._runtime_archive_kind(str(target["id"])) not in {"heartbeat", "webhook"})

    def finish_episode_relations(self, episode_id: str, ordinal: int,
                                 decisions: list[dict[str, str]], candidate_ids: set[str],
                                 *, evidence_records: dict[str, dict] | None = None) -> None:
        now = time.time()
        source = self.episode(episode_id)
        if source is None or int(source["summarized_through_ordinal"]) != ordinal:
            raise ValueError("episode summary changed during relation review")
        if len(decisions) > 4:
            raise ValueError("too many episode relations")
        seen: set[str] = set()
        eligible_candidates: dict[str, int] = {}
        for target_id in candidate_ids:
            target = self.episode(target_id)
            if target is not None and self.episode_relation_target_eligible(source, target):
                eligible_candidates[target_id] = int(target["summarized_through_ordinal"])
        for index, item in enumerate(decisions):
            target_id = item["target_episode_id"]
            if target_id not in candidate_ids or target_id in seen:
                raise ValueError("unknown or duplicate relation target")
            target = self.episode(target_id)
            if target is None or not self.episode_relation_target_eligible(source, target):
                raise ValueError("relation must point to an older episode")
            if item["relation"] not in {"follows_up", "revises", "context"}:
                raise ValueError("invalid relation")
            if any(not item[key].strip() or len(item[key]) > 300
                   for key in ("explanation", "source_evidence", "target_evidence")):
                raise ValueError("relation requires short evidence on both sides")
            source_fields = [str(source["narrative_summary"]), str(source["title"])]
            target_fields = [str(target["narrative_summary"]), str(target["title"])]
            if evidence_records is not None:
                source_fields = list(_evidence_text(evidence_records.get(episode_id, {})))
                target_fields = list(_evidence_text(evidence_records.get(target_id, {})))
            if not any(item["source_evidence"] in value for value in source_fields):
                raise ValueError(
                    f"relations[{index}].source_evidence 未出现在当前话题原文中："
                    f"{item['source_evidence']!r}；请从当前话题复制一段连续原文"
                )
            if not any(item["target_evidence"] in value for value in target_fields):
                raise ValueError(
                    f"relations[{index}].target_evidence 未出现在召回话题 {target_id} 原文中："
                    f"{item['target_evidence']!r}；请从该话题返回内容复制一段连续原文"
                )
            seen.add(target_id)
        with self._db:
            self._db.execute("DELETE FROM episode_relations WHERE source_episode_id=?", (episode_id,))
            self._db.execute("DELETE FROM episode_relation_reviews WHERE source_episode_id=?", (episode_id,))
            for item in decisions:
                self._db.execute(
                    """INSERT INTO episode_relations
                       (source_episode_id,target_episode_id,relation,explanation,evidence_json,
                        source_summary_ordinal,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (episode_id, item["target_episode_id"], item["relation"], item["explanation"],
                     json.dumps({"source": item["source_evidence"], "target": item["target_evidence"]}, ensure_ascii=False),
                     ordinal, now, now),
                )
            for target_id, target_ordinal in eligible_candidates.items():
                if target_id not in seen:
                    self._db.execute(
                        """INSERT INTO episode_relation_reviews
                           (source_episode_id,target_episode_id,decision,
                            source_summary_ordinal,target_summary_ordinal,reviewed_at)
                           VALUES (?,?,'unrelated',?,?,?)""",
                        (episode_id, target_id, ordinal, target_ordinal, now),
                    )
            self._db.execute(
                """UPDATE episode_relation_jobs SET processed_ordinal=?, claimed_at=NULL,
                   retry_at=0, failure_count=0, updated_at=? WHERE episode_id=?""",
                (ordinal, now, episode_id),
            )

    def release_episode_relation_candidate(self, episode_id: str, *, failed: bool = True) -> None:
        with self._db:
            row = self._db.execute("SELECT failure_count FROM episode_relation_jobs WHERE episode_id=?", (episode_id,)).fetchone()
            count = int(row[0]) + 1 if row and failed else 0
            self._db.execute(
                """UPDATE episode_relation_jobs SET claimed_at=NULL, failure_count=?, retry_at=?
                   WHERE episode_id=?""",
                (count, time.time() + min(3600, 60 * 2 ** min(count, 6)) if failed else 0, episode_id),
            )

    def next_episode_relation_retry_at(self) -> float | None:
        row = self._db.execute(
            """SELECT MIN(j.retry_at) FROM episode_relation_jobs j
               JOIN conversation_episodes e ON e.id=j.episode_id
               WHERE j.claimed_at IS NULL AND j.retry_at>0
                 AND e.summarized_through_ordinal>j.processed_ordinal"""
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None


def _evidence_text(value):
    """Use only text actually supplied to the relation model, including recall truncation."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"title", "summary", "content", "text", "assistant_text", "result", "arguments"}:
                if isinstance(child, str):
                    yield child
            if isinstance(child, (dict, list)):
                yield from _evidence_text(child)
    elif isinstance(value, list):
        for child in value:
            yield from _evidence_text(child)
