"""Indexing batches and cooperative polling, independent of application runtime."""
import asyncio
from collections.abc import Callable
from typing import Protocol

from ..storage.index_queue import IndexQueue


class DocumentEncoder(Protocol):
    async def encode(self, texts: list[str], *, query: bool) -> list[list[float]]: ...


class IndexAdapter(Protocol):
    def materialize(self, claim: dict[str, object]) -> int: ...
    def spaces(self) -> list[dict[str, object]]: ...
    def source_key(self, row: dict[str, object]) -> tuple[str, str]: ...
    def refresh(self, source_type: str, source_id: str, *, space_id: str | None = None) -> None: ...
    def activate_ready(self) -> None: ...
    def report(self, event: str, error: Exception, **fields: object) -> None: ...


class IndexWorker:
    def __init__(
        self, queue: IndexQueue, encoder: DocumentEncoder | None, adapter: IndexAdapter, *,
        document_batch_size: int, active_poll_seconds: float = .05, idle_poll_seconds: float = 2.0,
    ) -> None:
        self.queue = queue
        self.encoder = encoder
        self.adapter = adapter
        self.document_batch_size = document_batch_size
        self.active_poll_seconds = active_poll_seconds
        self.idle_poll_seconds = idle_poll_seconds

    async def maintain_once(self, *, allow_encoding: bool = True) -> bool:
        worked = False
        for claim in self.queue.claim_sources(16):
            try:
                changed = self.adapter.materialize(claim)
                worked = worked or bool(changed)
                self.adapter.refresh(str(claim["source_type"]), str(claim["source_id"]))
            except Exception as error:
                self.queue.fail_source(claim, error)
                self.adapter.report("materialize_failed", error, source_type=claim.get("source_type"))
        if not allow_encoding or self.encoder is None:
            return worked
        for space in self.adapter.spaces():
            rows = self.queue.claim_documents(str(space["id"]), self.document_batch_size)
            if not rows:
                continue
            worked = True
            try:
                vectors = await self.encoder.encode([str(row["content"]) for row in rows], query=False)
                sources = [self.adapter.source_key(row) for row in rows]
                self.queue.finish_documents(rows, vectors, int(space["dimensions"]), sources)
                for source_type, source_id in dict.fromkeys(sources):
                    self.adapter.refresh(source_type, source_id, space_id=str(space["id"]))
            except Exception as error:
                self.queue.fail_documents(rows, error)
                self.adapter.report("encode_failed", error, space_id=space["id"], batch_size=len(rows))
        self.adapter.activate_ready()
        return worked

    async def run(self, stop: asyncio.Event, *, busy: Callable[[], bool] = lambda: False) -> None:
        while not stop.is_set():
            try:
                worked = await self.maintain_once(allow_encoding=not busy())
            except asyncio.CancelledError:
                raise
            except Exception as error:
                worked = False
                self.adapter.report("worker_failed", error)
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=self.active_poll_seconds if worked else self.idle_poll_seconds,
                )
            except TimeoutError:
                pass
