"""Recallable memory eligibility and indexed text, without application sources."""
import time

from ..storage.index_records import IndexDocument
from ..storage.repository import MemoryRepository


class MemoryIndexSource:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    def eligible_ids(self) -> set[str]:
        return {str(row["id"]) for row in self.repository.recall_rows(now=time.time())}

    def documents(self, source_id: str) -> list[IndexDocument]:
        row = self.repository.recall_row(source_id)
        if row is None:
            return []
        return [IndexDocument(
            "confirmed_memory", source_id, "", 0,
            f"Kind: {row['kind']}\nKey: {row['key']}\nContent: {row['content']}",
        )]
