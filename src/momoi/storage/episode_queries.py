from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from .context_plans import recall_query_texts
from .context_plan_adapter import normalize_context_plan
from .episode_ranking import EpisodeRecallQuery, rank_episode_matches
from .episode_search import (
    EpisodeSearchDocument,
    EpisodeSearchField,
    EpisodeSearchMessage,
)
from .memory import estimate_tokens, token_chunk, truncate_tokens

if TYPE_CHECKING:
    from ..semantic import DenseRecallEvidence


class EpisodeQueryStore:
    def _episode_search_documents(
        self,
        *,
        after: float | None = None,
        before: float | None = None,
    ) -> tuple[dict[str, sqlite3.Row], list[EpisodeSearchDocument]]:
        time_filter = after is not None or before is not None
        rows = self._db.execute(
            """SELECT e.*, COALESCE((
                       SELECT MAX(t.updated_at) FROM episode_turns AS et
                       JOIN turns AS t ON t.id=et.turn_id
                       WHERE et.episode_id=e.id
                   ), e.updated_at) AS last_activity_at
               FROM conversation_episodes AS e"""
        ).fetchall()
        rows_by_id = {str(row["id"]): row for row in rows}
        message_rows = self._db.execute(
            """SELECT et.episode_id, et.ordinal, et.relation, et.unit_ids_json,
                      m.id, m.turn_id, m.role, m.content, m.created_at,
                      m.delivery_state
               FROM episode_turns AS et
               JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE (m.role IN ('user', 'event') OR m.delivery_state IN
                      ('delivered', 'uncertain', 'internal'))
                 AND (? IS NULL OR m.created_at>=?)
                 AND (? IS NULL OR m.created_at<?)
               ORDER BY et.episode_id, et.ordinal, m.id""",
            (after, after, before, before),
        ).fetchall()
        active_plans = {
            str(row["turn_id"]): normalize_context_plan(
                json.loads(str(row["plan_json"]))
            )
            for row in self._db.execute(
                """SELECT cp.turn_id, cp.plan_json
                   FROM context_plans AS cp
                   JOIN (
                       SELECT turn_id, MAX(revision) AS revision
                       FROM context_plans
                       WHERE state<>'superseded'
                       GROUP BY turn_id
                   ) AS active
                     ON active.turn_id=cp.turn_id
                    AND active.revision=cp.revision"""
            ).fetchall()
        }
        messages_by_episode: dict[str, list[EpisodeSearchMessage]] = {}
        for message in message_rows:
            episode_id = str(message["episode_id"])
            turn_id = str(message["turn_id"])
            plan = active_plans.get(turn_id, {})
            units = {
                str(unit.get("id")): unit
                for unit in plan.get("intent_units", [])
                if isinstance(unit, dict) and unit.get("id")
            }
            unit_ids = json.loads(str(message["unit_ids_json"]))
            scoped_units = [
                units[unit_id]
                for unit_id in unit_ids
                if isinstance(unit_id, str) and unit_id in units
            ]
            scoped_text = "\n".join(
                str(value)
                for unit in scoped_units
                for value in (
                    unit.get("text"),
                    unit.get("intent"),
                    " ".join(str(item) for item in unit.get("references", [])),
                    " ".join(
                        text
                        for item in unit.get("recall_queries", [])
                        for text in recall_query_texts(item)
                    ),
                )
                if value
            )
            content = str(message["content"])
            searchable_text = content
            if scoped_text:
                if str(message["role"]) in {"user", "event"}:
                    searchable_text = scoped_text
                elif str(message["relation"]) != "primary":
                    searchable_text = ""
            messages_by_episode.setdefault(episode_id, []).append(
                EpisodeSearchMessage(
                    id=int(message["id"]),
                    turn_id=turn_id,
                    ordinal=int(message["ordinal"]),
                    relation=str(message["relation"]),
                    role=str(message["role"]),
                    content=content,
                    created_at=float(message["created_at"]),
                    delivery_state=str(message["delivery_state"]),
                    timestamp=self.context_timestamp(message["created_at"]),
                    searchable_text=searchable_text,
                    scoped=bool(scoped_text),
                )
            )
        documents: list[EpisodeSearchDocument] = []
        for episode_id, row in rows_by_id.items():
            messages = tuple(messages_by_episode.get(episode_id, []))
            if time_filter and not messages:
                continue
            fields = (
                ()
                if time_filter
                else (
                    EpisodeSearchField("title", str(row["title"] or "")),
                    EpisodeSearchField(
                        "working_summary", str(row["working_summary"] or "")
                    ),
                    EpisodeSearchField(
                        "summary",
                        (
                            str(row["summary"] or "")
                            if "summary" in row.keys()
                            else ""
                        ),
                    ),
                    EpisodeSearchField(
                        "narrative_summary",
                        str(row["narrative_summary"] or ""),
                    ),
                    *(
                        EpisodeSearchField("topic", str(value))
                        for value in json.loads(str(row["topics_json"] or "[]"))
                    ),
                    *(
                        EpisodeSearchField("entity", str(value))
                        for value in json.loads(str(row["entities_json"] or "[]"))
                    ),
                    *(
                        EpisodeSearchField("open_loop", str(value))
                        for value in json.loads(str(row["open_loops_json"] or "[]"))
                    ),
                )
            )
            documents.append(
                EpisodeSearchDocument(
                    episode_id=episode_id,
                    fields=fields,
                    last_activity_at=(
                        max(message.created_at for message in messages)
                        if time_filter
                        else float(row["last_activity_at"])
                    ),
                    salience=float(row["salience"]),
                    messages=messages,
                ),
            )
        return rows_by_id, documents

    def _ranked_episode_results(
        self,
        queries: list[EpisodeRecallQuery],
        max_results: int,
        *,
        after: float | None = None,
        before: float | None = None,
        offset: int = 0,
        minimum_confidence: float | None = None,
        dense_evidence: DenseRecallEvidence | None = None,
    ) -> list[dict[str, object]]:
        if max_results <= 0 or offset < 0 or not queries:
            return []
        rows_by_id, documents = self._episode_search_documents(
            after=after,
            before=before,
        )
        matches = self._episode_query.match_many(
            [query.expression for query in queries],
            documents,
        )
        hits = rank_episode_matches(
            queries,
            matches,
            documents,
            limit=max_results,
            offset=offset,
            **(
                {"minimum_confidence": minimum_confidence}
                if minimum_confidence is not None
                else {}
            ),
            dense_evidence=dense_evidence,
        )
        results: list[dict[str, object]] = []
        for hit in hits:
            row = rows_by_id.get(hit.episode_id)
            if row is None:
                continue
            episode = self._episode_dict(row)
            episode["last_activity_at"] = hit.last_activity_at
            episode["last_activity_timestamp"] = self.context_timestamp(
                hit.last_activity_at
            )
            episode["matches"] = [
                {
                    key: getattr(match, key)
                    for key in (
                        "id",
                        "turn_id",
                        "ordinal",
                        "relation",
                        "role",
                        "created_at",
                        "delivery_state",
                        "timestamp",
                    )
                }
                | {"content": truncate_tokens(match.content, 500)}
                for match in hit.matches
            ]
            episode["matched_keywords"] = list(hit.matched_keywords)
            episode["keyword_match_count"] = len(hit.matched_keywords)
            episode["search_score"] = hit.score
            episode["semantic_score"] = hit.semantic_score
            episode["relevance_confidence"] = hit.relevance_confidence
            episode["channels"] = list(hit.channels)
            episode["dense_cosine"] = hit.dense_cosine
            episode["agreement_bonus"] = hit.agreement_bonus
            episode["corroboration_bonus"] = hit.corroboration_bonus
            episode["dense_only"] = hit.dense_only
            episode["matched_queries"] = [
                {
                    "expression": query.expression,
                    "unit_ids": list(query.unit_ids),
                    "priority": query.priority,
                    "score": query.score,
                    "matched_alternatives": list(query.matched_alternatives),
                    "alternative_count": query.alternative_count,
                    "field_matches": list(query.field_matches),
                    "message_ids": list(query.message_ids),
                    "scoped_message_ids": list(query.scoped_message_ids),
                    "turn_ids": list(query.turn_ids),
                }
                for query in hit.matched_queries
            ]
            results.append(episode)
        return results

    def search_episode_queries(
        self,
        queries: list[EpisodeRecallQuery],
        max_results: int,
        *,
        after: float | None = None,
        before: float | None = None,
        offset: int = 0,
        dense_evidence: DenseRecallEvidence | None = None,
    ) -> list[dict[str, object]]:
        return self._ranked_episode_results(
            queries,
            max_results,
            after=after,
            before=before,
            offset=offset,
            dense_evidence=dense_evidence,
        )

    def search_episodes(
        self,
        query: str,
        max_results: int,
        *,
        after: float | None = None,
        before: float | None = None,
        offset: int = 0,
        dense_evidence: DenseRecallEvidence | None = None,
    ) -> list[dict[str, object]]:
        if max_results <= 0 or offset < 0:
            return []
        if query.strip():
            return self._ranked_episode_results(
                [EpisodeRecallQuery(query.strip())],
                max_results,
                after=after,
                before=before,
                offset=offset,
                minimum_confidence=0.0,
                dense_evidence=dense_evidence,
            )
        rows = self._db.execute(
            """SELECT e.*, COALESCE((
                       SELECT MAX(t.updated_at) FROM episode_turns AS et
                       JOIN turns AS t ON t.id=et.turn_id
                       WHERE et.episode_id=e.id
                   ), e.updated_at) AS last_activity_at
               FROM conversation_episodes AS e
               WHERE (? IS NULL AND ? IS NULL) OR EXISTS (
                   SELECT 1 FROM episode_turns AS et
                   JOIN messages AS m ON m.turn_id=et.turn_id
                   WHERE et.episode_id=e.id
                     AND (? IS NULL OR m.created_at>=?)
                     AND (? IS NULL OR m.created_at<?)
               )""",
            (after, before, after, after, before, before),
        ).fetchall()
        ranked = [(float(row["last_activity_at"]), row) for row in rows]
        ranked.sort(key=lambda item: item[0], reverse=True)
        results = []
        for _, row in ranked[offset : offset + max_results]:
            episode = self._episode_dict(row)
            episode["last_activity_timestamp"] = self.context_timestamp(
                row["last_activity_at"]
            )
            episode["matches"] = []
            results.append(episode)
        return results

    def conversation_message(
        self,
        episode_id: str,
        message_id: int,
        content_offset: int = 0,
        token_budget: int = 30000,
    ) -> dict[str, object] | None:
        row = self._db.execute(
            """SELECT m.id, m.turn_id, et.ordinal, m.role, m.content, m.created_at,
                      m.delivery_state
               FROM episode_turns AS et
               JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE et.episode_id=? AND m.id=?""",
            (episode_id, message_id),
        ).fetchone()
        if row is None:
            return None
        content, next_offset = token_chunk(
            str(row["content"]), content_offset, token_budget
        )
        return {
            **{
                name: row[name]
                for name in (
                    "id",
                    "turn_id",
                    "ordinal",
                    "role",
                    "created_at",
                    "delivery_state",
                )
            },
            "timestamp": self.context_timestamp(row["created_at"]),
            "content": content,
            "content_offset": content_offset,
            "next_content_offset": next_offset,
        }

    def conversation_episode(
        self,
        episode_id: str,
        token_budget: int = 30000,
        *,
        before_ordinal: int | None = None,
        after: float | None = None,
        before: float | None = None,
    ) -> dict[str, object] | None:
        episode = self.episode(episode_id)
        if episode is None:
            return None
        archived = self._db.execute(
            """SELECT et.ordinal, m.content FROM episode_turns AS et
               JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE et.episode_id=?
                 AND (? IS NULL OR et.ordinal<?)
                 AND (? IS NULL OR m.created_at>=?)
                 AND (? IS NULL OR m.created_at<?)""",
            (
                episode_id,
                before_ordinal,
                before_ordinal,
                after,
                after,
                before,
                before,
            ),
        ).fetchall()
        messages = self.episode_messages(
            episode_id,
            token_budget,
            before_ordinal=before_ordinal,
            include_nondelivered=True,
            after=after,
            before=before,
        )
        omitted_messages = len(messages) < len(archived)
        content_truncated = (
            sum(estimate_tokens(str(row["content"])) for row in archived) > token_budget
        )
        next_before_ordinal = (
            min(int(message["ordinal"]) for message in messages)
            if omitted_messages and messages
            else None
        )
        return {
            **episode,
            "messages": messages,
            "truncated": omitted_messages or content_truncated,
            "next_before_ordinal": next_before_ordinal,
            "window_first_timestamp": min(
                (str(message["timestamp"]) for message in messages),
                default=None,
            ),
            "window_last_timestamp": max(
                (str(message["timestamp"]) for message in messages),
                default=None,
            ),
        }
