"""Documents supplied by an index source adapter."""
import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class IndexDocument:
    document_type: str
    source_id: str
    parent_id: str
    chunk_index: int
    content: str
    source_ids: tuple[object, ...] = ()
    starts_at: float | None = None
    ends_at: float | None = None

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()
