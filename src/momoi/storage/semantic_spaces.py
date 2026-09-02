from __future__ import annotations

import time
import uuid

from .semantic_documents import (
    DOCUMENT_TEMPLATE_VERSION,
    QUERY_TEMPLATE_VERSION,
    SEMANTIC_PROVIDER,
)


class SemanticSpaceStore:
    def semantic_space(self, *, state: str = "active") -> dict[str, object] | None:
        row = self._db.execute(
            "SELECT * FROM semantic_spaces WHERE state=? ORDER BY created_at DESC LIMIT 1",
            (state,),
        ).fetchone()
        return dict(row) if row else None

    def ensure_semantic_space(
        self,
        *,
        model: str,
        dimensions: int,
        calibration_profile: str,
        state: str = "building",
    ) -> dict[str, object]:
        row = self._db.execute(
            """SELECT * FROM semantic_spaces
               WHERE provider=? AND model=? AND dimensions=?
                 AND query_template_version=? AND document_template_version=?
                 AND calibration_profile=?""",
            (
                SEMANTIC_PROVIDER,
                model,
                dimensions,
                QUERY_TEMPLATE_VERSION,
                DOCUMENT_TEMPLATE_VERSION,
                calibration_profile,
            ),
        ).fetchone()
        if row is not None:
            if state == "building" and row["state"] == "retired":
                with self._db:
                    self._db.execute(
                        "UPDATE semantic_spaces SET state='building', activated_at=NULL WHERE id=?",
                        (row["id"],),
                    )
                row = self._db.execute(
                    "SELECT * FROM semantic_spaces WHERE id=?", (row["id"],)
                ).fetchone()
            return dict(row)
        now = time.time()
        space_id = f"sem-{uuid.uuid4().hex}"
        with self._db:
            self._db.execute(
                """INSERT INTO semantic_spaces
                   (id, provider, model, dimensions, query_template_version,
                    document_template_version, calibration_profile, state, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    space_id,
                    SEMANTIC_PROVIDER,
                    model,
                    dimensions,
                    QUERY_TEMPLATE_VERSION,
                    DOCUMENT_TEMPLATE_VERSION,
                    calibration_profile,
                    state,
                    now,
                ),
            )
        return dict(
            self._db.execute(
                "SELECT * FROM semantic_spaces WHERE id=?", (space_id,)
            ).fetchone()
        )

    def activate_semantic_space(self, space_id: str) -> None:
        status = self.semantic_status(space_id)
        if status["pending"] or status["encoding"] or status["retry"]:
            raise ValueError("semantic space still has unfinished documents")
        if status["eligible_source_coverage"] < 1.0:
            raise ValueError("semantic space source coverage is incomplete")
        now = time.time()
        with self._db:
            row = self._db.execute(
                "SELECT state FROM semantic_spaces WHERE id=?", (space_id,)
            ).fetchone()
            if row is None:
                raise ValueError("semantic space not found")
            self._db.execute(
                "UPDATE semantic_spaces SET state='retired' WHERE state='active' AND id<>?",
                (space_id,),
            )
            self._db.execute(
                "UPDATE semantic_spaces SET state='active', activated_at=? WHERE id=?",
                (now, space_id),
            )

    def semantic_status(self, space_id: str | None = None) -> dict[str, object]:
        if space_id is None:
            row = self._db.execute(
                """SELECT * FROM semantic_spaces
                   ORDER BY CASE state WHEN 'active' THEN 0 WHEN 'building' THEN 1 ELSE 2 END,
                            created_at DESC LIMIT 1"""
            ).fetchone()
        else:
            row = self._db.execute(
                "SELECT * FROM semantic_spaces WHERE id=?", (space_id,)
            ).fetchone()
        if row is None:
            return {"state": "absent", "eligible_source_coverage": 0.0}
        counts = {
            str(item["state"]): int(item["count"])
            for item in self._db.execute(
                """SELECT state, COUNT(*) AS count FROM semantic_documents
                   WHERE space_id=? GROUP BY state""",
                (row["id"],),
            )
        }
        eligible = self._eligible_source_ids()
        total_sources = sum(len(values) for values in eligible.values())
        covered = 0
        for source_type, source_ids in eligible.items():
            if not source_ids:
                continue
            if source_type == "episode":
                rows = self._db.execute(
                    """SELECT DISTINCT CASE WHEN document_type='episode_turn'
                                             THEN parent_id ELSE source_id END AS id
                       FROM semantic_documents WHERE space_id=?
                         AND document_type IN ('episode_summary','episode_turn')
                         AND state='ready'""",
                    (row["id"],),
                ).fetchall()
            else:
                rows = self._db.execute(
                    """SELECT DISTINCT source_id AS id FROM semantic_documents
                       WHERE space_id=? AND document_type=? AND state='ready'""",
                    (row["id"], source_type),
                ).fetchall()
            covered += len(source_ids.intersection(str(item["id"]) for item in rows))
        dirty = self._db.execute(
            "SELECT COUNT(*) AS count, MIN(changed_at) AS oldest FROM semantic_dirty_sources"
        ).fetchone()
        return {
            **dict(row),
            **{
                state: counts.get(state, 0)
                for state in ("ready", "pending", "encoding", "retry", "inactive")
            },
            "eligible_sources": total_sources,
            "covered_sources": covered,
            "eligible_source_coverage": covered / total_sources
            if total_sources
            else 1.0,
            "dirty_sources": int(dirty["count"]),
            "oldest_dirty_at": dirty["oldest"],
        }
