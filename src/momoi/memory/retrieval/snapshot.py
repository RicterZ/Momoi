from dataclasses import dataclass

import numpy as np

from ..storage.vector_repository import VectorRepository
from ..storage.vectors import decode_vector


@dataclass(frozen=True)
class VectorMetadata:
    key: tuple[str, str, int]
    document_type: str
    source_id: str
    parent_id: str
    starts_at: float | None
    ends_at: float | None
    generation: int


@dataclass(frozen=True)
class _VectorSegment:
    vectors: np.ndarray
    metadata: tuple[VectorMetadata, ...]


class SegmentedVectorSnapshot:
    def __init__(self, repository: VectorRepository, dimensions: int) -> None:
        self.repository = repository
        self.dimensions = dimensions
        self.space_id = ""
        self._segments: list[_VectorSegment] = []
        self._latest: dict[tuple[str, str, int], int] = {}
        self._by_parent: dict[str, set[tuple[str, str, int]]] = {}
        self._generation = 0

    def load(self, space_id: str) -> None:
        self.space_id = space_id
        self._segments = []
        self._latest = {}
        self._by_parent = {}
        self._generation = 0
        for rows in self.repository.ready_documents(space_id):
            self._append(rows)

    def _append(self, rows: list[dict[str, object]]) -> None:
        vectors: list[np.ndarray] = []
        metadata: list[VectorMetadata] = []
        for row in rows:
            key = (
                str(row["document_type"]),
                str(row["source_id"]),
                int(row["chunk_index"]),
            )
            try:
                if int(row["dimensions"] or 0) != self.dimensions:
                    raise ValueError("stored embedding dimension mismatch")
                vector = decode_vector(row["vector"], self.dimensions)
            except ValueError as error:
                self.repository.invalidate(self.space_id, *key, str(error))
                continue
            self._generation += 1
            generation = self._generation
            self._latest[key] = generation
            parent_id = str(row["parent_id"] or "")
            if parent_id:
                self._by_parent.setdefault(parent_id, set()).add(key)
            vectors.append(vector)
            metadata.append(
                VectorMetadata(
                    key,
                    key[0],
                    key[1],
                    parent_id,
                    float(row["starts_at"]) if row["starts_at"] is not None else None,
                    float(row["ends_at"]) if row["ends_at"] is not None else None,
                    generation,
                )
            )
        if vectors:
            self._segments.append(
                _VectorSegment(np.ascontiguousarray(np.stack(vectors)), tuple(metadata))
            )

    def replace_source(
        self, document_type: str, source_id: str, *, include_children: bool = False,
    ) -> None:
        parent_keys = self._by_parent.get(source_id, ())
        stale = [
            key
            for key in self._latest
            if (include_children and key in parent_keys)
            or (key[0] == document_type and key[1] == source_id)
        ]
        for key in stale:
            self._latest.pop(key, None)
        rows = self.repository.ready_source_documents(
            self.space_id, document_type, source_id, include_children=include_children
        )
        self._append(rows)
        if len(self._segments) > 64:
            self.load(self.space_id)

    def search(
        self,
        query_vectors: np.ndarray,
        document_types: set[str],
        limit: int,
        *,
        after: float | None = None,
        before: float | None = None,
        group_by_parent: bool = False,
        source_ids: frozenset[str] | None = None,
    ) -> dict[int, list[tuple[VectorMetadata, float]]]:
        candidates: dict[int, list[tuple[VectorMetadata, float]]] = {
            index: [] for index in range(len(query_vectors))
        }
        width = max(1, limit)
        for segment in self._segments:
            # Filter before top-k: stale or other-pool vectors must not consume slots.
            eligible = [
                index for index, meta in enumerate(segment.metadata)
                if meta.document_type in document_types
                and (source_ids is None or meta.source_id in source_ids)
                and self._latest.get(meta.key) == meta.generation
                and (after is None or meta.ends_at is not None and meta.ends_at >= after)
                and (before is None or meta.starts_at is not None and meta.starts_at < before)
            ]
            if not eligible:
                continue
            scores = query_vectors @ segment.vectors[eligible].T
            for query_index in range(scores.shape[0]):
                row_scores = scores[query_index]
                if group_by_parent:
                    indices = range(len(row_scores))
                else:
                    take = min(width, len(row_scores))
                    indices = np.argpartition(row_scores, -take)[-take:]
                for index in indices:
                    meta = segment.metadata[eligible[int(index)]]
                    candidates[query_index].append((meta, float(row_scores[index])))
        for query_index, hits in candidates.items():
            hits.sort(key=lambda item: item[1], reverse=True)
            if group_by_parent:
                best: dict[tuple[str, str], tuple[VectorMetadata, float]] = {}
                for meta, score in hits:
                    key = (meta.parent_id or meta.source_id, meta.document_type)
                    if key not in best:
                        best[key] = (meta, score)
                hits = sorted(best.values(), key=lambda item: item[1], reverse=True)
            candidates[query_index] = hits[:width]
        return candidates
