"""Memory reranking contracts and bounded candidate selection.

The caller supplies model transport. Candidates can share a model request with
other evidence pools without coupling memory to those pools or their storage.
"""
from collections.abc import Sequence
from typing import Protocol


class SelectionProtocolError(ValueError):
    """The model's selection cannot be mapped to the supplied candidates."""


class MemoryReranker(Protocol):
    async def __call__(
        self, request: str, candidates: list[dict[str, object]],
    ) -> list[dict[str, object]]: ...


def select_indices(rows, indices, *, name):
    if (not isinstance(indices, list) or len(indices) > len(rows)
            or any(type(i) is not int or not 0 <= i < len(rows) for i in indices)
            or len(set(indices)) != len(indices)):
        raise SelectionProtocolError(f"Invalid or duplicate {name}")
    return [rows[i] for i in indices]


def validate_reranked_rows(candidates, selected):
    """A reranker may omit/reorder candidates, never invent or rewrite memories."""
    if not isinstance(selected, list):
        raise SelectionProtocolError("reranker must return a list")
    by_id = {(row["source"], row["id"]): row for row in candidates}
    seen = set()
    result = []
    for row in selected:
        if (not isinstance(row, dict) or type(row.get("id")) is not int
                or not isinstance(row.get("source"), str)):
            raise SelectionProtocolError("reranker returned an invalid memory")
        key = (row.get("source"), row.get("id"))
        if key not in by_id or key in seen or row != by_id[key]:
            raise SelectionProtocolError("reranker changed, duplicated, or invented a memory")
        seen.add(key)
        result.append(by_id[key])
    return result


class MemoryRerankCandidates:
    def __init__(self, candidates: Sequence[dict[str, object]]):
        self.confirmed = [row for row in candidates if row.get("source") == "confirmed"]
        self.reflections = [row for row in candidates if row.get("source") == "reflection"]

    def payload(self):
        return {
            "memories": [{"index": i, "kind": row.get("kind"), "key": row.get("key"),
                          "content": row.get("content")}
                         for i, row in enumerate(self.confirmed)],
            "reflections": [{"index": i, "kind": row.get("kind"), "key": row.get("key"),
                             "content": row.get("content"), "local_date": row.get("local_date"),
                             "confidence": row.get("confidence"), "evidence": row.get("evidence")}
                            for i, row in enumerate(self.reflections)],
        }

    def schema(self):
        return {
            name: {"type": "array", "maxItems": len(rows), "uniqueItems": True,
                   "items": {"type": "integer", "minimum": 0, "maximum": max(0, len(rows)-1)}}
            for name, rows in (("memory_indices", self.confirmed),
                               ("reflection_indices", self.reflections))
        }

    def select(self, arguments):
        return (
            select_indices(self.confirmed, arguments.get("memory_indices"), name="memory_indices"),
            select_indices(self.reflections, arguments.get("reflection_indices"), name="reflection_indices"),
        )
