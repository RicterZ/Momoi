import sqlite3
from pathlib import Path

import pytest

from momoi.memory.storage.vector_repository import VectorRepository
from momoi.runtime import MomoiDaemon
from momoi.storage import Store
from tests.support import config, install_scripted_replyer

SCHEMA = Path(__file__).parents[1] / "src/momoi/storage/core/schema.sql"


def _create_tables(db: sqlite3.Connection, *tables: str) -> None:
    text = SCHEMA.read_text()
    for table in tables:
        start = text.index(f"CREATE TABLE IF NOT EXISTS {table} (")
        db.execute(text[start:text.index(";", start) + 1])


@pytest.fixture
def database(tmp_path):
    db = sqlite3.connect(tmp_path / "memory.sqlite3")
    db.row_factory = sqlite3.Row
    # Use the actual schema, but only memory data and commit receipts.
    # No events, turns, goals, journal, or application bootstrap are available.
    _create_tables(db, "memories", "memory_evidence", "memory_tombstones", "memory_commits")
    db.execute("CREATE TABLE application_work (id TEXT PRIMARY KEY)")
    yield db
    db.close()


@pytest.fixture
def vectors(database):
    _create_tables(database, "semantic_documents")
    return VectorRepository(database)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "memory.sqlite3")
    yield value
    value.close()


@pytest.fixture
def daemon(tmp_path):
    value = MomoiDaemon(config(
        tmp_path, max_tokens=1000, memory_results=6, timezone="Asia/Shanghai",
        system_prompt="companion soul sentinel", database=tmp_path / "daemon.sqlite3",
    ))
    install_scripted_replyer(value)
    yield value
    value.store.close()
