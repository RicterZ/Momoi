from dataclasses import dataclass

import numpy as np

from ..storage import Store, decode_vector


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
    def __init__(self, store: Store, dimensions: int) -> None:
        self.store = store
        self.dimensions = dimensions
        self.space_id = ""
        self._segments: list[_VectorSegment] = []
        self._latest: dict[tuple[str, str, int], int] = {}
        self._generation = 0

    def load(self, space_id: str) -> None:
        self.space_id = space_id
        self._segments = []
        self._latest = {}
        self._generation = 0
        for rows in self.store.semantic_ready_documents(space_id):
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
                self.store.invalidate_semantic_document(self.space_id, *key, str(error))
                continue
            self._generation += 1
            generation = self._generation
            self._latest[key] = generation
            vectors.append(vector)
            metadata.append(
                VectorMetadata(
                    key,
                    key[0],
                    key[1],
                    str(row["parent_id"] or ""),
                    float(row["starts_at"]) if row["starts_at"] is not None else None,
                    float(row["ends_at"]) if row["ends_at"] is not None else None,
                    generation,
                )
            )
        if vectors:
            self._segments.append(
                _VectorSegment(np.ascontiguousarray(np.stack(vectors)), tuple(metadata))
            )

    def replace_source(self, source_type: str, source_id: str) -> None:
        stale = [
            key
            for key in self._latest
            if (
                key[0] in {"episode_summary", "episode_turn"}
                and source_type == "episode"
                and any(
                    meta.key == key and meta.parent_id == source_id
                    for segment in self._segments
                    for meta in segment.metadata
                )
            )
            or (key[0] == source_type and key[1] == source_id)
            or (
                key[0] == "episode_summary"
                and source_type == "episode"
                and key[1] == source_id
            )
        ]
        for key in stale:
            self._latest.pop(key, None)
        rows = self.store.semantic_ready_source_documents(
            self.space_id, source_type, source_id
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
    ) -> dict[int, list[tuple[VectorMetadata, float]]]:
        candidates: dict[int, list[tuple[VectorMetadata, float]]] = {
            index: [] for index in range(len(query_vectors))
        }
        width = max(1, limit)
        for segment in self._segments:
            scores = query_vectors @ segment.vectors.T
            for query_index in range(scores.shape[0]):
                row_scores = scores[query_index]
                if document_types.issubset({"episode_summary", "episode_turn"}):
                    indices = range(len(row_scores))
                else:
                    take = min(width, len(row_scores))
                    indices = np.argpartition(row_scores, -take)[-take:]
                for index in indices:
                    meta = segment.metadata[int(index)]
                    if meta.document_type not in document_types:
                        continue
                    if self._latest.get(meta.key) != meta.generation:
                        continue
                    if after is not None and (
                        meta.ends_at is None or meta.ends_at < after
                    ):
                        continue
                    if before is not None and (
                        meta.starts_at is None or meta.starts_at >= before
                    ):
                        continue
                    candidates[query_index].append((meta, float(row_scores[index])))
        for query_index, hits in candidates.items():
            hits.sort(key=lambda item: item[1], reverse=True)
            if document_types.issubset({"episode_summary", "episode_turn"}):
                best: dict[tuple[str, str], tuple[VectorMetadata, float]] = {}
                for meta, score in hits:
                    key = (meta.parent_id or meta.source_id, meta.document_type)
                    if key not in best:
                        best[key] = (meta, score)
                hits = sorted(best.values(), key=lambda item: item[1], reverse=True)
            candidates[query_index] = hits[:width]
        return candidates
