"""Momoi source ownership, index activation policy, and indexing observability."""
import logging

from ...memory.retrieval.dense import DenseQueryService
from ...memory.retrieval.snapshot import SegmentedVectorSnapshot
from ...observability.events import log_event
from ...storage import Store
from ...storage.semantic.semantic_documents import semantic_source_key

logger = logging.getLogger(__name__)


class MomoiIndexAdapter:
    def __init__(
        self, store: Store, snapshot: SegmentedVectorSnapshot, queries: DenseQueryService, *,
        auto_activate: bool = True,
    ) -> None:
        self.store = store
        self.snapshot = snapshot
        self.queries = queries
        self.auto_activate = auto_activate

    def materialize(self, claim: dict[str, object]) -> int:
        return self.store.materialize_semantic_source(claim)

    def spaces(self) -> list[dict[str, object]]:
        return [
            space for state in ("building", "active")
            if (space := self.store.semantic_space(state=state)) is not None
        ]

    @staticmethod
    def source_key(row: dict[str, object]) -> tuple[str, str]:
        return semantic_source_key(row)

    def refresh(self, source_type: str, source_id: str, *, space_id: str | None = None) -> None:
        if not self.snapshot.space_id or space_id is not None and space_id != self.snapshot.space_id:
            return
        self.snapshot.replace_source(
            "episode_summary" if source_type == "episode" else source_type,
            source_id, include_children=source_type == "episode",
        )

    def activate_ready(self) -> None:
        building = self.store.semantic_space(state="building")
        if building is None or not self.auto_activate:
            return
        status = self.store.semantic_status(str(building["id"]))
        if (
            status["eligible_source_coverage"] >= 1.0
            and not status["pending"] and not status["encoding"]
            and not status["retry"] and not status["dirty_sources"]
        ):
            self.store.activate_semantic_space(str(building["id"]))
            self.snapshot.load(str(building["id"]))
            self.queries.unavailable_reason = ""
            log_event(
                logger, logging.INFO, "semantic_space_activated",
                space_id=building["id"], model=building["model"],
            )

    @staticmethod
    def report(event: str, error: Exception, **fields: object) -> None:
        log_event(
            logger, logging.ERROR, f"semantic_{event}", error_type=type(error).__name__,
            **fields, **({"exc_info": True} if event == "worker_failed" else {}),
        )
