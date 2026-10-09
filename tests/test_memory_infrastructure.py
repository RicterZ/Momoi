"""The extraction must remain usable without the surrounding application."""
import shutil
import subprocess
import sys
from pathlib import Path


def test_memory_infrastructure_runs_outside_momoi(tmp_path):
    source = Path(__file__).parents[1] / "src" / "momoi" / "memory"
    package = tmp_path / "standalone_memory"
    shutil.copytree(source, package)
    schema = (source.parent / "storage/core/schema.sql").read_text()
    statements = []
    for table in ("memories", "memory_tombstones", "memory_evidence", "memory_commits"):
        start = schema.index(f"CREATE TABLE IF NOT EXISTS {table} (")
        statements.append(schema[start:schema.index(";", start) + 1])
    (tmp_path / "memory.sql").write_text("\n".join(statements))
    script = '''
import importlib.util
import sys
from pathlib import Path

package = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "standalone_memory", package / "__init__.py",
    submodule_search_locations=[str(package)],
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module

# Fail even if an editable Momoi install happens to be visible to the subprocess.
class RejectApplicationImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "momoi" or fullname.startswith("momoi."):
            raise AssertionError("memory depends on application: " + fullname)
sys.meta_path.insert(0, RejectApplicationImports())
spec.loader.exec_module(module)

from standalone_memory import Memory, MemoryPlan, PlanningContext

from standalone_memory.retrieval.models import MemoryRecallQuery, DenseThresholds
from standalone_memory.retrieval.sparse import StringSearchBackend, search_expression
from standalone_memory.text import estimate_tokens, token_chunk
from standalone_memory.storage.vectors import encode_vector, decode_vector
from standalone_memory.storage.repository import MemoryRepository
from standalone_memory.storage.index_documents import IndexDocuments
from standalone_memory.storage.index_queue import IndexQueue
from standalone_memory.storage.index_records import IndexDocument
from standalone_memory.indexing.worker import IndexWorker
from standalone_memory.storage.vector_repository import VectorRepository
from standalone_memory.retrieval.snapshot import SegmentedVectorSnapshot
from standalone_memory.retrieval.dense import DenseQueryService, DenseSearchPool, MemoryVectorRecall
from standalone_memory.retrieval.service import MemoryRecallService
from standalone_memory.retrieval.rerank import MemoryRerankCandidates
import sqlite3
import asyncio

with sqlite3.connect(":memory:") as db:
    db.row_factory = sqlite3.Row
    db.executescript((package.parent / "memory.sql").read_text())
    memory = Memory(db)
    async def planner(context):
        return {"decisions": [{"operation_ids": ["one"], "action": "defer", "reason": "Need clarification"}]}
    planned = asyncio.run(memory.plan(
        [{"id": "one", "type": "add", "event_id": "event", "content": "coffee", "evidence": "coffee"}],
        evidence={"event": "coffee"}, planner=planner,
    ))
    assert isinstance(planned, MemoryPlan)
    assert planned.decisions[0]["action"] == "defer"
    assert memory.snapshots([]) == {}
    assert MemoryRepository(db).snapshots([]) == {}
    assert MemoryRecallService(MemoryRepository(db)).rank([], 6) == []
    snapshot = SegmentedVectorSnapshot(VectorRepository(db), 2)
    engine = DenseQueryService(snapshot, None)
    assert asyncio.run(engine.search(["饮品"], [DenseSearchPool({"confirmed_memory"})])).fallback_reason == "no_active_space"
    assert MemoryVectorRecall(engine, {}) is not None
    assert IndexDocument("confirmed_memory", "1", "", 0, "text").content_sha256
    worker = IndexWorker(IndexQueue(db), None, None, document_batch_size=4)
    async def stopped_worker():
        stop = asyncio.Event()
        stop.set()
        await worker.run(stop)
    asyncio.run(stopped_worker())
assert MemoryRerankCandidates([]).select({"memory_indices": [], "reflection_indices": []}) == ([], [])

assert MemoryRecallQuery("咖啡").dense_expression == "咖啡"
assert DenseThresholds(0.5, 0.7, 0.9).calibrated(0.9) == 1.0
assert search_expression("无糖咖啡", ["用户喜欢无糖咖啡"], StringSearchBackend())
text = "用户喜欢无糖咖啡。" * 20
chunk, offset = token_chunk(text, 0, 20)
assert offset is not None and estimate_tokens(chunk) <= 20
assert decode_vector(encode_vector([3.0, 4.0], 2), 2).tolist() == [
    0.6000000238418579, 0.800000011920929,
]
'''
    subprocess.run(
        [sys.executable, "-I", "-c", script, str(package)],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    )


def test_legacy_imports_share_the_extracted_objects():
    from momoi.memory import text
    from momoi.memory.retrieval import models, sparse as search
    from momoi.memory.storage import vectors
    from momoi.runtime.agent import budget
    from momoi.semantic import models as semantic_models
    from momoi.storage.core import search as storage_search
    from momoi.storage.memory import memory_values
    from momoi.storage.semantic import semantic_documents

    assert storage_search.SearchBackend is search.SearchBackend
    assert storage_search.search_expression is search.search_expression
    assert memory_values.MemoryRecallQuery is models.MemoryRecallQuery
    assert semantic_models.DenseThresholds is models.DenseThresholds
    assert semantic_models.DenseMemoryHit is models.DenseMemoryHit
    assert semantic_models.DenseEpisodeHit is models.DenseEpisodeHit
    assert budget.TextSizer is text.TextSizer
    assert budget.MEMORY_TEXT_FITTER is text.MEMORY_TEXT_FITTER
    assert budget.TEXT_SIZER is text.TEXT_SIZER
    assert memory_values.token_chunk is text.token_chunk
    assert semantic_documents.encode_vector is vectors.encode_vector
    assert semantic_documents.decode_vector is vectors.decode_vector
