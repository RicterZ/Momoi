from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..policies import MemoryPolicy
from ..search import (
    alternative_weights,
    document_frequency,
    search_alternatives,
    search_expression,
)
from ..models import (
    IncomingMessage,
    MemoryCandidate,
    MemoryForgetCandidate,
)
from .episode_ranking import rank_recall_items

if TYPE_CHECKING:
    from ..semantic import DenseRecallEvidence


MEMORY_KINDS = {
    "profile",
    "preference",
    "relationship",
    "shared",
    "episodic",
    "routine",
}
MEMORY_ACTIVATIONS = {"always", "recent", "recall"}
ALWAYS_MEMORY_KINDS = {"profile", "preference", "relationship"}
RECENT_MEMORY_WINDOW_SECONDS = 30 * 24 * 60 * 60
_DEFAULT_MEMORY_POLICY = MemoryPolicy()
_MEMORY_QUERY_PRIORITY_WEIGHTS = (1.0, 0.5, 0.3)
_MEMORY_SECOND_ALIAS_WEIGHT = 0.18
_MEMORY_THIRD_ALIAS_WEIGHT = 0.08
_MEMORY_SECOND_QUERY_WEIGHT = 0.55
_MEMORY_THIRD_QUERY_WEIGHT = 0.2
_MEMORY_RECENCY_FLOOR = 0.8
_MEMORY_RECENCY_HALF_LIFE_SECONDS = 180 * 86400
_CONFIRMED_MEMORY_SCORE_FLOOR = 0.35
_REFLECTION_MEMORY_SCORE_FLOOR = 0.35
MAX_MEMORY_RECALL_RESULTS = 6
REFLECTION_MEMORY_CAUTION = (
    "Daily reflection memories are fallible and may be outdated or no longer "
    "applicable; use them only as supporting context and prefer current evidence."
)


@dataclass(frozen=True)
class MemoryRecallQuery:
    expression: str
    unit_ids: tuple[str, ...] = ()
    priority: int = 0
    semantic_expression: str = ""

    @property
    def dense_expression(self) -> str:
        return self.semantic_expression.strip() or self.expression.strip()


def memory_snapshot_fingerprint(memory: Mapping[str, object]) -> str:
    payload = {
        key: memory.get(key)
        for key in (
            "id",
            "kind",
            "key",
            "content",
            "activation",
            "expires_at",
            "source_event_id",
            "evidence_quote",
            "updated_at",
            "superseded_by",
        )
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def format_reflection_memory(row: Mapping[str, object]) -> str:
    local_date = str(row.get("local_date") or "unknown")
    return (
        f"- [date={local_date} {row['kind']}:{row['key']}] "
        f"{row['content']}"
    )


def memory_expires_at(
    activation: str,
    ttl_hours: float,
    now: float,
    policy: MemoryPolicy = _DEFAULT_MEMORY_POLICY,
) -> float | None:
    if activation != "recent":
        return None
    hours = min(
        policy.recent_max_ttl_hours,
        max(policy.recent_min_ttl_hours, float(ttl_hours)),
    )
    return now + hours * 3600


def estimate_tokens(text: str) -> int:
    from ..runtime.budget import TEXT_SIZER

    return TEXT_SIZER.estimate(text)


def truncate_tokens(text: str, token_budget: int) -> str:
    from ..runtime.budget import MEMORY_TEXT_FITTER

    return MEMORY_TEXT_FITTER.truncate(text, token_budget)


def token_chunk(text: str, offset: int, token_budget: int) -> tuple[str, int | None]:
    if token_budget <= 0:
        raise ValueError("token budget must be positive")
    if offset < 0 or offset > len(text):
        raise ValueError("content offset is outside the message")
    remaining = text[offset:]
    if estimate_tokens(remaining) <= token_budget:
        return remaining, None
    marker = "…[continued]"
    if estimate_tokens(marker) >= token_budget:
        marker = ""
    low, high = 0, len(remaining)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(remaining[:middle] + marker) <= token_budget:
            low = middle
        else:
            high = middle - 1
    if low == 0:
        low = 1
    return remaining[:low] + marker, offset + low


class MemoryStore:
    _memory_policy: MemoryPolicy

    def maintenance_memory_inventory(self) -> list[dict[str, object]]:
        self.purge_expired_memories()
        now = time.time()
        cutoff = now - RECENT_MEMORY_WINDOW_SECONDS
        rows = self._db.execute(
            """SELECT id, kind, key, content, activation, authority,
                      source_event_id, evidence_quote, importance,
                      created_at, updated_at, expires_at, superseded_by
               FROM memories AS m
               WHERE m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at>?)
                 AND (m.activation<>'recent' OR m.updated_at>=?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )
               ORDER BY m.id""",
            (now, cutoff),
        ).fetchall()
        return [dict(row) for row in rows]

    def purge_expired_memories(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        cutoff = now - RECENT_MEMORY_WINDOW_SECONDS
        rows = self._db.execute(
            """SELECT id FROM memories AS m
               WHERE m.superseded_by IS NULL
                 AND (m.expires_at IS NOT NULL AND m.expires_at <= ?
                      OR m.activation='recent' AND m.expires_at IS NULL
                         AND m.updated_at < ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )""",
            (now, cutoff),
        ).fetchall()
        if not rows:
            return 0
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        self._db.execute(
            f"DELETE FROM memory_evidence WHERE memory_id IN ({placeholders})",
            ids,
        )
        self._db.execute(
            f"DELETE FROM memories WHERE id IN ({placeholders})", ids
        )
        self._db.commit()
        return len(ids)

    def _memory_rows(
        self, activation: str, *, now: float | None = None
    ) -> list[sqlite3.Row]:
        if activation not in MEMORY_ACTIVATIONS:
            raise ValueError("invalid memory activation")
        now = time.time() if now is None else now
        recent_cutoff = now - RECENT_MEMORY_WINDOW_SECONDS
        return self._db.execute(
            """SELECT id, kind, key, content, activation, importance, updated_at
               FROM memories AS m
               WHERE m.activation=? AND m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND (m.activation<>'recent' OR m.updated_at>=?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )
               ORDER BY m.importance DESC, m.updated_at DESC, m.id DESC""",
            (activation, now, recent_cutoff),
        ).fetchall()

    @staticmethod
    def _compact_memory_context(
        label: str, rows: list[sqlite3.Row]
    ) -> str:
        if not rows:
            return ""
        contents: list[str] = []
        seen: set[str] = set()
        for row in rows:
            content = " ".join(str(row["content"]).split())
            if not content or content in seen:
                continue
            seen.add(content)
            contents.append(content)
        if not contents:
            return ""
        return f"{label}：" + "；".join(contents)

    def always_memory_context(self) -> str:
        return self._compact_memory_context(
            "老师的长期记忆", self._memory_rows("always")
        )

    def recent_memory_context(self) -> str:
        self.purge_expired_memories()
        return self._compact_memory_context(
            "老师近期需要保持的上下文", self._memory_rows("recent")
        )

    def _alternative_weights(
        self,
        query: str,
        documents: list[tuple[str, ...]],
    ) -> dict[str, float]:
        return alternative_weights(
            document_frequency(
                search_alternatives(query), documents, self._search_backend
            ),
            len(documents),
        )

    def rank_recalled_memories(
        self,
        queries: list[MemoryRecallQuery],
        max_results: int,
        *,
        now: float | None = None,
        dense_evidence: DenseRecallEvidence | None = None,
    ) -> list[dict[str, object]]:
        """Rank confirmed and reflection memory in independent bounded pools."""

        if max_results <= 0 or not queries:
            return []
        limit = min(MAX_MEMORY_RECALL_RESULTS, max_results)
        stamp = time.time() if now is None else now
        self.purge_expired_memories()
        confirmed_rows = self._db.execute(
            """SELECT id, kind, key, content, importance, updated_at
               FROM memories
               WHERE superseded_by IS NULL
                 AND activation='recall'
                 AND (expires_at IS NULL OR expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=memories.kind AND t.key=memories.key
                 )""",
            (stamp,),
        ).fetchall()
        reflection_rows = self._db.execute(
            """SELECT rm.id, rm.kind, rm.key, rm.content, rm.confidence,
                      rm.updated_at, r.local_date
               FROM reflection_memories AS rm
               LEFT JOIN reflections AS r ON r.id=rm.source_reflection_id
               ORDER BY rm.updated_at DESC"""
        ).fetchall()

        confirmed_candidates: list[dict[str, object]] = []
        for row in confirmed_rows:
            importance = min(1.0, max(0.0, float(row["importance"])))
            confirmed_candidates.append(
                {
                    "source": "confirmed",
                    "id": int(row["id"]),
                    "kind": str(row["kind"]),
                    "key": str(row["key"]),
                    "content": str(row["content"]),
                    "confidence": 1.0,
                    "reliability_bonus": 0.12 + 0.03 * importance,
                    "recency_floor": 1.0,
                    "updated_at": float(row["updated_at"]),
                }
            )
        reflection_candidates: list[dict[str, object]] = []
        for row in reflection_rows:
            confidence = min(1.0, max(0.0, float(row["confidence"])))
            reflection_candidates.append(
                {
                    "source": "reflection",
                    "id": int(row["id"]),
                    "kind": str(row["kind"]),
                    "key": str(row["key"]),
                    "content": str(row["content"]),
                    "local_date": str(row["local_date"] or "unknown"),
                    "confidence": confidence,
                    "reliability_bonus": 0.06 * confidence,
                    "recency_floor": _MEMORY_RECENCY_FLOOR,
                    "updated_at": float(row["updated_at"]),
                }
            )
        return [
            *self._rank_memory_pool(
                queries,
                confirmed_candidates,
                limit,
                _CONFIRMED_MEMORY_SCORE_FLOOR,
                stamp,
                dense_evidence,
            ),
            *self._rank_memory_pool(
                queries,
                reflection_candidates,
                limit,
                _REFLECTION_MEMORY_SCORE_FLOOR,
                stamp,
                dense_evidence,
            ),
        ]

    def _rank_memory_pool(
        self,
        queries: list[MemoryRecallQuery],
        candidates: list[dict[str, object]],
        limit: int,
        score_floor: float,
        now: float,
        dense_evidence: DenseRecallEvidence | None = None,
    ) -> list[dict[str, object]]:
        if not candidates or limit <= 0:
            return []
        documents = [
            (str(item["key"]), str(item["content"])) for item in candidates
        ]
        states: list[dict[str, object]] = [
            {
                "unit_scores": {},
                "eligibility_scores": [],
                "evidence_signals": [],
                "matched_queries": [],
                "unit_ids": set(),
                "channels": set(),
                "dense_cosines": [],
                "agreement_bonus": 0.0,
            }
            for _ in candidates
        ]
        for query in queries:
            weights = self._alternative_weights(query.expression, documents)
            priority_weight = _MEMORY_QUERY_PRIORITY_WEIGHTS[
                min(
                    max(0, int(query.priority)),
                    len(_MEMORY_QUERY_PRIORITY_WEIGHTS) - 1,
                )
            ]
            for index, document in enumerate(documents):
                match = search_expression(
                    query.expression,
                    document,
                    self._search_backend,
                    weights=weights,
                )
                document_type = (
                    "confirmed_memory"
                    if candidates[index]["source"] == "confirmed"
                    else "reflection_memory"
                )
                dense_hit = (
                    dense_evidence.memory.get(query.dense_expression, {}).get(
                        (document_type, str(candidates[index]["id"]))
                    )
                    if dense_evidence is not None
                    else None
                )
                if match is None and dense_hit is None:
                    continue
                alias_scores = (
                    sorted(
                        (
                            float(weights.get(alternative, 1.0))
                            for alternative in match.alternatives
                        ),
                        reverse=True,
                    )
                    if match is not None
                    else []
                )
                sparse_score = alias_scores[0] if alias_scores else 0.0
                if len(alias_scores) > 1:
                    sparse_score += _MEMORY_SECOND_ALIAS_WEIGHT * alias_scores[1]
                if len(alias_scores) > 2:
                    sparse_score += _MEMORY_THIRD_ALIAS_WEIGHT * alias_scores[2]
                cosine = float(dense_hit.cosine) if dense_hit is not None else None
                thresholds = (
                    dense_evidence.thresholds(document_type)
                    if dense_evidence is not None
                    else None
                )
                dense_score = (
                    thresholds.calibrated(cosine)
                    if thresholds is not None and cosine is not None
                    else 0.0
                )
                normalized_sparse = 1.0 - math.exp(-sparse_score)
                agreement = (
                    min(normalized_sparse, dense_score)
                    if sparse_score > 0 and dense_score > 0
                    else 0.0
                )
                hybrid_score = sparse_score + 0.55 * dense_score + 0.20 * agreement
                score = hybrid_score * priority_weight
                state = states[index]
                eligibility_scores = state["eligibility_scores"]
                assert isinstance(eligibility_scores, list)
                if sparse_score > 0:
                    eligibility_scores.append(sparse_score)
                signals = state["evidence_signals"]
                assert isinstance(signals, list)
                signals.append((sparse_score, cosine, hybrid_score, thresholds))
                unit_scores = state["unit_scores"]
                assert isinstance(unit_scores, dict)
                units = query.unit_ids or (f"query:{query.dense_expression}",)
                for unit_id in units:
                    unit_scores.setdefault(unit_id, []).append(score)
                matched_queries = state["matched_queries"]
                assert isinstance(matched_queries, list)
                matched_queries.append(query.dense_expression)
                unit_ids = state["unit_ids"]
                assert isinstance(unit_ids, set)
                unit_ids.update(query.unit_ids)
                channels = state["channels"]
                assert isinstance(channels, set)
                if sparse_score > 0:
                    channels.add("sparse")
                if cosine is not None:
                    channels.add("dense")
                    dense_cosines = state["dense_cosines"]
                    assert isinstance(dense_cosines, list)
                    dense_cosines.append(cosine)
                state["agreement_bonus"] = float(state["agreement_bonus"]) + 0.20 * agreement

        ranked_candidates: list[dict[str, object]] = []
        for candidate, state in zip(candidates, states, strict=True):
            unit_scores = state["unit_scores"]
            assert isinstance(unit_scores, dict)
            if not unit_scores:
                continue
            semantic_score = 0.0
            for scores in unit_scores.values():
                ordered = sorted((float(value) for value in scores), reverse=True)
                semantic_score += ordered[0]
                if len(ordered) > 1:
                    semantic_score += _MEMORY_SECOND_QUERY_WEIGHT * ordered[1]
                if len(ordered) > 2:
                    semantic_score += _MEMORY_THIRD_QUERY_WEIGHT * ordered[2]
            if len(unit_scores) > 1:
                semantic_score *= 1.0 + min(0.3, 0.1 * (len(unit_scores) - 1))

            updated_at = float(candidate["updated_at"])
            age = max(0.0, now - updated_at)
            recency_floor = float(candidate["recency_floor"])
            recency_factor = recency_floor + (
                1.0 - recency_floor
            ) * math.exp(
                -math.log(2.0)
                * age
                / _MEMORY_RECENCY_HALF_LIFE_SECONDS
            )
            confidence = float(candidate["confidence"])
            search_score = (
                semantic_score * recency_factor
                + float(candidate["reliability_bonus"])
            )
            eligibility_scores = state["eligibility_scores"]
            assert isinstance(eligibility_scores, list)
            sparse_eligibility = (
                max(float(value) for value in eligibility_scores)
                * recency_factor
                + float(candidate["reliability_bonus"])
                if eligibility_scores
                else 0.0
            )
            signals = state["evidence_signals"]
            assert isinstance(signals, list)
            dense_admitted = False
            support_admitted = False
            hybrid_eligibility = sparse_eligibility
            for sparse_score, cosine, hybrid_score, thresholds in signals:
                if cosine is None or thresholds is None:
                    continue
                if sparse_score <= 0 and cosine >= thresholds.only:
                    dense_admitted = True
                if (
                    sparse_score > 0
                    and cosine >= thresholds.support
                    and hybrid_score * recency_factor
                    + float(candidate["reliability_bonus"]) >= score_floor
                ):
                    support_admitted = True
                hybrid_eligibility = max(
                    hybrid_eligibility,
                    hybrid_score * recency_factor
                    + float(candidate["reliability_bonus"]),
                )
            if (
                sparse_eligibility < score_floor
                and not dense_admitted
                and not support_admitted
            ):
                continue
            channels = state["channels"]
            assert isinstance(channels, set)
            dense_cosines = state["dense_cosines"]
            assert isinstance(dense_cosines, list)
            ranked_candidates.append(
                {
                    **{
                        key: value
                        for key, value in candidate.items()
                        if key not in {"reliability_bonus", "recency_floor"}
                    },
                    "semantic_score": semantic_score,
                    "search_score": search_score,
                    "eligibility_score": hybrid_eligibility,
                    "score_floor": score_floor,
                    "last_activity_at": updated_at,
                    "salience": confidence,
                    "matched_queries": list(
                        dict.fromkeys(str(value) for value in state["matched_queries"])
                    ),
                    "unit_ids": sorted(str(value) for value in state["unit_ids"]),
                    "channels": sorted(str(value) for value in channels),
                    "dense_cosine": max(dense_cosines) if dense_cosines else None,
                    "agreement_bonus": float(state["agreement_bonus"]),
                    "dense_only": dense_admitted and not eligibility_scores,
                }
            )
        return rank_recall_items(ranked_candidates, now=now)[:limit]

    def ranked_memory_context(
        self,
        query: str,
        max_results: int,
    ) -> tuple[str, str]:
        """Render independently ranked confirmed and reflection result sets."""

        if max_results <= 0 or not query.strip():
            return "", ""
        ranked = self.rank_recalled_memories(
            [MemoryRecallQuery(query.strip())],
            max_results,
        )
        confirmed: list[str] = []
        reflected: list[str] = []
        reflection_header = REFLECTION_MEMORY_CAUTION
        for row in ranked:
            line = (
                format_reflection_memory(row)
                if row["source"] == "reflection"
                else f"- [{row['kind']}:{row['key']}] {row['content']}"
            )
            if row["source"] == "confirmed":
                confirmed.append(line)
            else:
                reflected.append(line)
        return (
            "\n".join(confirmed),
            "\n".join([reflection_header, *reflected]) if reflected else "",
        )

    def search_memories(
        self,
        query: str,
        max_results: int,
        *,
        include_core: bool = False,
        activation: str | None = None,
    ) -> list[dict[str, object]]:
        if max_results <= 0:
            return []
        if activation is not None and activation not in MEMORY_ACTIVATIONS:
            raise ValueError("invalid memory activation")
        self.purge_expired_memories()
        rows = self._db.execute(
            """SELECT id, kind, key, content, authority, evidence_quote,
                      activation, importance, updated_at,
                      (SELECT COUNT(*) FROM memory_evidence AS e
                       WHERE e.memory_id=memories.id) AS evidence_count
               FROM memories
               WHERE superseded_by IS NULL
                 AND (expires_at IS NULL OR expires_at > ?)
                 AND (activation<>'recent' OR updated_at>=?)
                 AND (? IS NULL OR activation=?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=memories.kind AND t.key=memories.key
                 )""",
            (time.time(), time.time() - RECENT_MEMORY_WINDOW_SECONDS, activation, activation),
        ).fetchall()
        core_kinds = {"profile", "relationship", "shared"}
        ranked: list[tuple[float, sqlite3.Row]] = []
        documents = [(str(row["key"]), str(row["content"])) for row in rows]
        weights = self._alternative_weights(query, documents)
        for row, document in zip(rows, documents):
            match = search_expression(
                query,
                document,
                self._search_backend,
                weights=weights,
            )
            core = include_core and row["kind"] in core_kinds
            if not core and match is None:
                continue
            score = (
                (match.score if match else 0.0)
                + float(row["importance"]) * 0.1
                + (1.0 if core else 0.0)
            )
            ranked.append((score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [dict(row) for _, row in ranked[:max_results]]

    def has_memory(self, kind: str, key: str) -> bool:
        return (
            self._db.execute(
                """SELECT 1 FROM memories AS m
               WHERE m.kind=? AND m.key=? AND m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )""",
                (kind, key, time.time()),
            ).fetchone()
            is not None
        )

    def active_memory(self, kind: str, key: str) -> dict[str, object] | None:
        row = self._db.execute(
            """SELECT id, kind, key, content, importance FROM memories AS m
               WHERE m.kind=? AND m.key=? AND m.superseded_by IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM memory_tombstones AS t
                     WHERE t.kind=m.kind AND t.key=m.key
                 )
               ORDER BY m.id DESC LIMIT 1""",
            (kind, key, time.time()),
        ).fetchone()
        return dict(row) if row else None

    def _remember(
        self,
        memory: MemoryCandidate,
        events: list[IncomingMessage],
        now: float,
    ) -> None:
        source_event = next(
            (event for event in events if memory.evidence in event.text), None
        )
        if (
            memory.kind not in MEMORY_KINDS
            or memory.activation not in MEMORY_ACTIVATIONS
            or not all((memory.key, memory.content, memory.evidence))
            or source_event is None
            or len(memory.key) > 200
            or len(memory.content) > 2000
            or len(memory.evidence) > 500
        ):
            return
        source_event_id = source_event.event_id
        self._db.execute(
            "DELETE FROM memory_tombstones WHERE kind=? AND key=?",
            (memory.kind, memory.key),
        )
        old = self._db.execute(
            """SELECT id, content FROM memories
               WHERE kind=? AND key=? AND superseded_by IS NULL
               ORDER BY id DESC LIMIT 1""",
            (memory.kind, memory.key),
        ).fetchone()
        expires_at = memory_expires_at(
            memory.activation, memory.ttl_hours, now, self._memory_policy
        )
        if old and old["content"] == memory.content:
            self._db.execute(
                """UPDATE memories SET source_event_id=?, evidence_quote=?,
                   activation=?, expires_at=?, importance=MAX(importance, ?),
                   updated_at=?
                   WHERE id=?""",
                (
                    source_event_id,
                    memory.evidence,
                    memory.activation,
                    expires_at,
                    memory.importance,
                    now,
                    old["id"],
                ),
            )
            self._add_memory_evidence(
                int(old["id"]), source_event_id, memory.evidence, now
            )
            return
        cursor = self._db.execute(
            """INSERT INTO memories
               (kind, key, content, activation, authority, source_event_id,
                evidence_quote, importance, created_at, updated_at, expires_at)
               VALUES (?, ?, ?, ?, 'owner', ?, ?, ?, ?, ?, ?)""",
            (
                memory.kind,
                memory.key,
                memory.content,
                memory.activation,
                source_event_id,
                memory.evidence,
                memory.importance,
                now,
                now,
                expires_at,
            ),
        )
        if old:
            self._db.execute(
                "UPDATE memories SET superseded_by=?, updated_at=? WHERE id=?",
                (cursor.lastrowid, now, old["id"]),
            )
        self._add_memory_evidence(
            int(cursor.lastrowid), source_event_id, memory.evidence, now
        )
    def _add_memory_evidence(
        self,
        memory_id: int,
        source_event_id: str,
        quote: str,
        now: float,
    ) -> None:
        self._db.execute(
            """INSERT OR IGNORE INTO memory_evidence
               (memory_id, source_event_id, quote, created_at)
               VALUES (?, ?, ?, ?)""",
            (memory_id, source_event_id, quote, now),
        )

    def _forget_memory(
        self,
        memory: MemoryForgetCandidate,
        events: list[IncomingMessage],
        now: float,
    ) -> None:
        source_event = next(
            (event for event in events if memory.evidence in event.text), None
        )
        if source_event is None:
            return
        self._db.execute(
            """INSERT INTO memory_tombstones
               (kind, key, source_event_id, evidence_quote, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(kind, key) DO UPDATE SET
                 source_event_id=excluded.source_event_id,
                 evidence_quote=excluded.evidence_quote,
                 created_at=excluded.created_at""",
            (memory.kind, memory.key, source_event.event_id, memory.evidence, now),
        )
