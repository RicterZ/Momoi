import json
import sqlite3
import time

from .context_plan_adapter import normalize_context_plan, normalize_context_retrieval
from .integrity import decode_stored_json


def recall_query_texts(value: object) -> list[str]:
    if not isinstance(value, dict):
        text = " ".join(str(value or "").split())
        return [text] if text else []
    semantic = " ".join(str(value.get("semantic") or "").split())
    keywords = [
        " ".join(str(item).split())
        for item in value.get("keywords") or []
        if " ".join(str(item).split())
    ]
    return list(dict.fromkeys([semantic, *keywords])) if semantic else keywords


class ContextPlanStore:
    """Versioned Owner recall plans, retrieval evidence, and reuse lineage."""

    @staticmethod
    def _context_plan_dict(row: sqlite3.Row) -> dict[str, object]:
        plan = dict(row)
        record_id = f"{row['turn_id']}:{row['revision']}"
        plan["source_event_ids"] = decode_stored_json(
            plan.pop("source_event_ids_json"),
            entity="context_plan",
            record_id=record_id,
            field="source_event_ids_json",
            expected_type=list,
        )
        normalized_plan = normalize_context_plan(
            decode_stored_json(
                plan.pop("plan_json"),
                entity="context_plan",
                record_id=record_id,
                field="plan_json",
                expected_type=dict,
            )
        )
        plan["plan"] = normalized_plan
        plan["retrieval"] = normalize_context_retrieval(
            decode_stored_json(
                plan.pop("retrieval_json"),
                entity="context_plan",
                record_id=record_id,
                field="retrieval_json",
                expected_type=dict,
            ),
            normalized_plan,
        )
        return plan

    def save_context_plan(
        self,
        turn_id: str,
        revision: int,
        source_event_ids: list[str],
        plan: dict[str, object],
        *,
        state: str = "planned",
    ) -> dict[str, object]:
        if revision < 1:
            raise ValueError("context plan revision must be positive")
        if state not in {"planned", "degraded"}:
            raise ValueError("a new context plan must be planned or degraded")
        source_json = json.dumps(
            source_event_ids, ensure_ascii=False, separators=(",", ":")
        )
        normalized_plan = normalize_context_plan(plan)
        plan_json = json.dumps(
            normalized_plan, ensure_ascii=False, separators=(",", ":")
        )
        now = time.time()
        with self._db:
            existing = self._db.execute(
                "SELECT * FROM context_plans WHERE turn_id=? AND revision=?",
                (turn_id, revision),
            ).fetchone()
            if existing is not None:
                if (
                    json.loads(str(existing["source_event_ids_json"]))
                    != source_event_ids
                    or normalize_context_plan(json.loads(str(existing["plan_json"])))
                    != normalized_plan
                ):
                    raise ValueError("context plan revision already exists")
                return self._context_plan_dict(existing)
            latest = self._db.execute(
                "SELECT MAX(revision) FROM context_plans WHERE turn_id=?", (turn_id,)
            ).fetchone()[0]
            if latest is not None and int(latest) >= revision:
                raise ValueError("context plan revision must increase")
            self._db.execute(
                """UPDATE context_plans SET state='superseded', updated_at=?
                   WHERE turn_id=? AND state<>'superseded'""",
                (now, turn_id),
            )
            self._db.execute(
                """INSERT INTO context_plans
                   (turn_id, revision, source_event_ids_json, plan_json,
                    state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (turn_id, revision, source_json, plan_json, state, now, now),
            )
        saved = self.context_plan(turn_id, revision)
        if saved is None:
            raise RuntimeError("context plan was not saved")
        return saved

    def context_plan(
        self, turn_id: str, revision: int | None = None
    ) -> dict[str, object] | None:
        if revision is None:
            row = self._db.execute(
                """SELECT * FROM context_plans
                   WHERE turn_id=? AND state<>'superseded'
                   ORDER BY revision DESC LIMIT 1""",
                (turn_id,),
            ).fetchone()
        else:
            row = self._db.execute(
                "SELECT * FROM context_plans WHERE turn_id=? AND revision=?",
                (turn_id, revision),
            ).fetchone()
        return self._context_plan_dict(row) if row else None

    def recall_reuse_candidates(
        self, turn_ids: list[str]
    ) -> list[dict[str, object]]:
        """Return only the latest recalled Turn and its effective search scope."""

        ordered_ids = [turn_id for turn_id in dict.fromkeys(turn_ids) if turn_id]
        if not ordered_ids:
            return []
        query_cache: dict[str, list[str]] = {}
        resolving: set[str] = set()

        def effective_queries(turn_id: str) -> list[str]:
            cached = query_cache.get(turn_id)
            if cached is not None:
                return cached
            if turn_id in resolving:
                return []
            resolving.add(turn_id)
            record = self.context_plan(turn_id)
            if record is None or record.get("state") != "recalled":
                queries: list[str] = []
            else:
                retrieval = record.get("retrieval")
                plan = record.get("plan")
                if not isinstance(retrieval, dict) or not isinstance(plan, dict):
                    queries = []
                else:
                    stored = retrieval.get("effective_recall_queries")
                    queries = [
                        text[:240]
                        for query in (stored if isinstance(stored, list) else [])
                        for text in recall_query_texts(query)[:1]
                    ]
                    units = [
                        unit
                        for unit in plan.get("intent_units") or []
                        if isinstance(unit, dict)
                    ]
                    for source_turn_id in dict.fromkeys(
                        str(unit.get("recall_from_turn_id") or "")
                        for unit in units
                        if unit.get("recall_from_turn_id")
                    ):
                        queries.extend(effective_queries(source_turn_id))
                    queries = list(dict.fromkeys(queries))
            resolving.discard(turn_id)
            query_cache[turn_id] = queries
            return queries

        for turn_id in reversed(ordered_ids):
            queries = effective_queries(turn_id)
            if queries:
                return [{"turn_id": turn_id, "queries": queries}]
        return []

    def save_context_retrieval(
        self,
        turn_id: str,
        revision: int,
        retrieval: dict[str, object],
        *,
        state: str = "recalled",
    ) -> dict[str, object]:
        if state not in {"recalled", "degraded"}:
            raise ValueError("context retrieval must be recalled or degraded")
        record = self.context_plan(turn_id, revision)
        if record is None or not isinstance(record.get("plan"), dict):
            raise ValueError("active context plan not found")
        normalized_retrieval = normalize_context_retrieval(retrieval, record["plan"])
        now = time.time()
        with self._db:
            cursor = self._db.execute(
                """UPDATE context_plans SET retrieval_json=?, state=?, updated_at=?
                   WHERE turn_id=? AND revision=? AND state<>'superseded'""",
                (
                    json.dumps(
                        normalized_retrieval,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    state,
                    now,
                    turn_id,
                    revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("active context plan not found")
        saved = self.context_plan(turn_id, revision)
        if saved is None:
            raise RuntimeError("context retrieval was not saved")
        return saved
