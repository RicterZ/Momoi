from __future__ import annotations

from .episode_cues import stored_cue_texts
from .execution_evidence import execution_turns, journal_rows, search_fields

import json
import re
import sqlite3
from typing import TYPE_CHECKING

from ..context.context_plans import recall_query_texts
from ..context.context_plan_adapter import normalize_context_plan
from .episode_ranking import EpisodeRecallQuery, rank_episode_matches
from .episode_search import (
    EpisodeSearchDocument,
    EpisodeSearchField,
    EpisodeSearchMessage,
)
from ..memory.memory_values import estimate_tokens, token_chunk, truncate_tokens

if TYPE_CHECKING:
    from ...semantic.models import DenseRecallEvidence


def keyword_sentence_excerpt(content: str, keyword: str, max_chars: int = 100):
    """Keep the hit sentence and at most one whole sentence on either side."""
    max_chars = max(1, min(100, max_chars))
    match = re.search(re.escape(keyword), content, re.IGNORECASE)
    anchor, hit_end = (match.start(), match.end()) if match else (0, 0)
    boundaries = [0, *(m.end() for m in re.finditer(r"[。！？!?；;\n]|\.(?=\s|$)", content))]
    if boundaries[-1] != len(content):
        boundaries.append(len(content))
    index = next((i for i in range(len(boundaries) - 1)
                  if boundaries[i] <= anchor < boundaries[i + 1]), 0)
    start, end = boundaries[index:index + 2] if len(boundaries) > 1 else (0, 0)
    if end - start > max_chars:
        start = max(start, anchor - max(0, (max_chars - (hit_end - anchor)) // 2))
        end = min(len(content), start + max_chars)
    else:
        if index > 0 and end - boundaries[index - 1] <= max_chars:
            start = boundaries[index - 1]
        if index + 2 < len(boundaries) and boundaries[index + 2] - start <= max_chars:
            end = boundaries[index + 2]
    return start, end


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
        execution_fields = search_fields(journal_rows(self._db, after=after, before=before))
        documents: list[EpisodeSearchDocument] = []
        for episode_id, row in rows_by_id.items():
            messages = tuple(messages_by_episode.get(episode_id, []))
            if time_filter and not messages and episode_id not in execution_fields:
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
                        EpisodeSearchField("recall_cue", str(value))
                        for value in stored_cue_texts(str(row["recall_cues_json"] or "[]"))
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
                    fields=(*fields, EpisodeSearchField("execution", execution_fields.get(episode_id, ""))),
                    last_activity_at=(
                        max((message.created_at for message in messages), default=float(row["last_activity_at"]))
                        if time_filter
                        else float(row["last_activity_at"])
                    ),
                    messages=messages,
                ),
            )
        return rows_by_id, documents

    def _episode_topic_documents(self):
        """Search topic metadata and native execution evidence without expanding chat."""
        rows = self._db.execute("""SELECT e.*, COALESCE((
            SELECT MAX(t.updated_at) FROM episode_turns et JOIN turns t ON t.id=et.turn_id
            WHERE et.episode_id=e.id), e.updated_at) AS last_activity_at
            FROM conversation_episodes e""").fetchall()
        execution_fields = search_fields(journal_rows(self._db))
        documents = []
        for row in rows:
            fields = [EpisodeSearchField("title", row["title"]),
                      EpisodeSearchField("narrative_summary", row["narrative_summary"]),
                      EpisodeSearchField("summary", row["summary"])]
            for name, column in (("topic", "topics_json"), ("entity", "entities_json")):
                fields.extend(EpisodeSearchField(name, str(value)) for value in json.loads(row[column] or "[]"))
            fields.extend(EpisodeSearchField("recall_cue", text) for text in stored_cue_texts(row["recall_cues_json"]))
            fields.append(EpisodeSearchField("execution", execution_fields.get(str(row["id"]), "")))
            documents.append(EpisodeSearchDocument(str(row["id"]), tuple(fields),
                float(row["last_activity_at"]), ()))
        return {str(row["id"]): row for row in rows}, documents

    def _ranked_episode_results(
        self,
        queries: list[EpisodeRecallQuery],
        max_results: int,
        *,
        after: float | None = None,
        before: float | None = None,
        offset: int = 0,
        minimum_confidence: float | None = None,
        topics_only: bool = False,
        exclude_episode_ids: tuple[str, ...] = (),
        dense_evidence: DenseRecallEvidence | None = None,
    ) -> list[dict[str, object]]:
        if max_results <= 0 or offset < 0 or not queries:
            return []
        rows_by_id, documents = (self._episode_topic_documents()
            if topics_only and after is None and before is None else
            self._episode_search_documents(after=after, before=before))
        excluded = set(exclude_episode_ids)
        documents = [document for document in documents if document.episode_id not in excluded]
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
            for match in episode["matches"]:
                targets = self.message_quote_targets(match["id"])
                if targets:
                    match["quote_targets"] = targets
            episode["execution_evidence"] = execution_turns(
                self, hit.episode_id, hit.matched_keywords, limit=1, tool_limit=1,
                after=after, before=before)
            episode["matched_keywords"] = list(hit.matched_keywords)
            episode["keyword_match_count"] = len(hit.matched_keywords)
            episode["search_score"] = hit.score
            episode["semantic_score"] = hit.semantic_score
            episode["relevance_confidence"] = hit.relevance_confidence
            episode["channels"] = list(hit.channels)
            episode["dense_cosine"] = hit.dense_cosine
            episode["cue_cosine"] = hit.cue_cosine
            episode["admission_routes"] = list(hit.admission_routes)
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

    def topic_conversation_time(self, episode_id: str):
        row = self._db.execute(
            """SELECT MIN(m.created_at), MAX(m.created_at)
               FROM episode_turns e JOIN messages m ON m.turn_id=e.turn_id
               WHERE e.episode_id=?""", (episode_id,),
        ).fetchone()
        if row[0] is None:
            return None
        return {"start": self.context_timestamp(row[0]),
                "end": self.context_timestamp(row[1])}

    def episode_keyword_evidence(self, episode_id, keywords, *, limit=3, max_chars=100):
        """Hydrate selected topics only; never search model paraphrases as evidence."""
        terms = list(dict.fromkeys(str(term).strip() for term in keywords if str(term).strip()))
        rows = self._db.execute(
            """SELECT DISTINCT m.id, m.turn_id, m.role, m.content, m.created_at,
                       m.delivery_state
               FROM episode_turns et JOIN messages m ON m.turn_id=et.turn_id
               WHERE et.episode_id=? AND (m.role IN ('user','event')
                   OR (m.role='assistant' AND m.delivery_state IN ('delivered','uncertain')))
               ORDER BY m.id""", (episode_id,),
        ).fetchall() if terms else []
        hits = []
        for row in rows:
            content = str(row["content"] or "")
            matched = [term for term in terms if term.casefold() in content.casefold()]
            if matched:
                hits.append((dict(row), matched))
        # Query keywords are ordered: preserve the main lookup term before
        # broad location/entity terms; prefer original Owner evidence.
        hits.sort(key=lambda pair: (
            pair[0]["role"] != "user", min(terms.index(term) for term in pair[1]),
            -len(pair[1]), -pair[0]["id"],
        ))
        matches = []
        for row, matched in hits[:limit]:
            content = row["content"]
            start, end = keyword_sentence_excerpt(content, matched[0], max_chars)
            row.update(content=content[start:end], original_chars=len(content),
                       excerpt_start=start, excerpt_end=end,
                       timestamp=self.context_timestamp(row["created_at"]))
            targets = self.message_quote_targets(row["id"])
            if targets:
                row["quote_targets"] = targets
            matches.append(row)
        return {"matches": matches, "matched_message_count": len(hits),
                "matched_message_chars": sum(len(row["content"]) for row, _ in hits)}

    def search_topic_queries(self, queries, max_results, *, dense_evidence=None, minimum_confidence=None, exclude_episode_ids=()):
        return self._ranked_episode_results(queries, max_results,
            topics_only=True, dense_evidence=dense_evidence, minimum_confidence=minimum_confidence,
            exclude_episode_ids=exclude_episode_ids)

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
            **({"quote_targets": targets} if (targets := self.message_quote_targets(row["id"])) else {}),
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
        include_execution: bool = True,
        before_ordinal: int | None = None,
        after: float | None = None,
        before: float | None = None,
    ) -> dict[str, object] | None:
        episode = self.episode(episode_id)
        if episode is None:
            return None
        # Select dialogue Turns before loading message bodies or execution evidence.
        after_ordinal = 0
        older_dialogue = False
        if not include_execution:
            ordinals = self._db.execute(
                """SELECT DISTINCT et.ordinal FROM episode_turns et
                   JOIN messages m ON m.turn_id=et.turn_id
                   WHERE et.episode_id=? AND (? IS NULL OR et.ordinal<?)
                     AND (? IS NULL OR m.created_at>=?) AND (? IS NULL OR m.created_at<?)
                   ORDER BY et.ordinal DESC LIMIT 4""",
                (episode_id, before_ordinal, before_ordinal, after, after, before, before),
            ).fetchall()
            older_dialogue = len(ordinals) > 3
            if ordinals:
                after_ordinal = int(ordinals[min(2, len(ordinals) - 1)]['ordinal']) - 1
        archived = self._db.execute(
            """SELECT et.ordinal, m.content FROM episode_turns AS et
               JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE et.episode_id=? AND et.ordinal>?
                 AND (? IS NULL OR et.ordinal<?)
                 AND (? IS NULL OR m.created_at>=?)
                 AND (? IS NULL OR m.created_at<?)""",
            (
                episode_id,
                after_ordinal,
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
            after_ordinal=after_ordinal,
            include_nondelivered=True,
            after=after,
            before=before,
        )
        omitted_messages = older_dialogue or len(messages) < len(archived)
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
            **(execution_turns(self, episode_id, limit=10, tool_limit=12,
                               after=after, before=before, before_ordinal=before_ordinal,
                               selected_messages=messages) if include_execution else
               {"next_execution_cursor": 0}),
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
