"""The extraction must remain usable without the surrounding application."""
import shutil
import subprocess
import sys
from pathlib import Path


def test_memory_infrastructure_runs_outside_momoi(tmp_path):
    source = Path(__file__).parents[1] / "src" / "momoi" / "memory"
    package = tmp_path / "standalone_memory"
    shutil.copytree(source, package)
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
spec.loader.exec_module(module)

# Fail even if an editable Momoi install happens to be visible to the subprocess.
class RejectApplicationImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "momoi" or fullname.startswith("momoi."):
            raise AssertionError("memory depends on application: " + fullname)
sys.meta_path.insert(0, RejectApplicationImports())

from standalone_memory.retrieval.models import MemoryRecallQuery, DenseThresholds
from standalone_memory.retrieval.sparse import StringSearchBackend, search_expression
from standalone_memory.text import estimate_tokens, token_chunk
from standalone_memory.retrieval.vectors import encode_vector, decode_vector
from standalone_memory.storage.repository import MemoryRepository
from standalone_memory.retrieval.service import MemoryRecallService
from standalone_memory.retrieval.rerank import MemoryRerankCandidates
import sqlite3

with sqlite3.connect(":memory:") as db:
    assert MemoryRepository(db).snapshots([]) == {}
    assert MemoryRecallService(MemoryRepository(db)).rank([], 6) == []
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
    from momoi.memory.retrieval import models, sparse as search, vectors
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
