"""Retained memory index for writing; recall independently filters visible IDs."""

from ..storage.index_records import IndexDocument
from ..storage.repository import MemoryRepository


class MemoryIndexSource:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    def eligible_ids(self) -> set[str]:
        return {str(row["id"]) for row in self.repository.planning_rows()}

    def documents(self, source_id: str) -> list[IndexDocument]:
        rows = self.repository.planning_rows([source_id])
        if not rows:
            return []
        row = rows[0]
        return [IndexDocument(
            "confirmed_memory", source_id, "", 0,
            f"Kind: {row['kind']}\nKey: {row['key']}\nContent: {row['content']}",
        )]
