"""The only schema bootstrap entry point. Check compatibility before any DDL."""
from pathlib import Path
import sqlite3
from .migrations import apply_migrations, validate_schema_version
from ...memory.storage.transactions import transaction


def execute_schema(database: sqlite3.Connection, script: str) -> None:
    # executescript implicitly commits. Execute complete statements instead,
    # including trigger bodies, so bootstrap can roll back as one unit.
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            database.execute(statement)
            statement = ""
    if statement.strip() and not all(not line.strip() or line.lstrip().startswith("--") for line in statement.splitlines()):
        raise ValueError("incomplete schema statement")


def initialize_schema(database: sqlite3.Connection) -> None:
    validate_schema_version(database)
    if database.in_transaction:
        raise RuntimeError("schema initialization requires an idle connection")
    # Historical databases need additive baseline objects before old migrations.
    # This file is additive only; changes to existing objects require a migration.
    with transaction(database):
        execute_schema(database, Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
    apply_migrations(database)
