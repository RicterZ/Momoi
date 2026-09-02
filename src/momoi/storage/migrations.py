from __future__ import annotations

import sqlite3
from collections.abc import Callable


Migration = Callable[[sqlite3.Connection], None]


def _columns(database: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in database.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _add_runtime_archive_metadata(database: sqlite3.Connection) -> None:
    columns = _columns(database, "conversation_episodes")
    if "archive_kind" not in columns:
        database.execute(
            "ALTER TABLE conversation_episodes ADD COLUMN archive_kind TEXT"
        )
    if "archive_day" not in columns:
        database.execute(
            "ALTER TABLE conversation_episodes ADD COLUMN archive_day TEXT"
        )


def _add_turn_workflow_kind(database: sqlite3.Connection) -> None:
    if "workflow_kind" not in _columns(database, "turns"):
        database.execute(
            """ALTER TABLE turns ADD COLUMN workflow_kind TEXT CHECK (
                workflow_kind IN (
                    'owner', 'webhook', 'goal', 'heartbeat', 'reply_followup',
                    'reflection', 'memory_maintenance', 'episode_consolidate',
                    'episode_anneal'
                )
            )"""
        )


MIGRATIONS: tuple[Migration, ...] = (
    _add_runtime_archive_metadata,
    _add_turn_workflow_kind,
)
SCHEMA_VERSION = len(MIGRATIONS)


def apply_migrations(database: sqlite3.Connection) -> None:
    current = int(database.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {current} is newer than supported "
            f"version {SCHEMA_VERSION}"
        )
    for version, migration in enumerate(MIGRATIONS, start=1):
        if version <= current:
            continue
        with database:
            migration(database)
            database.execute(f"PRAGMA user_version={version}")
