from __future__ import annotations

import sqlite3
import re
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


def _add_memory_operation_workflow(database: sqlite3.Connection) -> None:
    sql = str(
        database.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='turns'"
        ).fetchone()[0]
    )
    if "'memory_operation'" in sql:
        return
    objects = [
        row[0]
        for row in database.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='turns' AND type IN ('index','trigger') AND sql IS NOT NULL"
        )
    ]
    replacement = re.sub(
        r'CREATE TABLE ["`\[]?turns["`\]]?', "CREATE TABLE turns_new", sql, count=1
    )
    replacement = replacement.replace(
        "'memory_maintenance'", "'memory_maintenance', 'memory_operation'"
    )
    database.commit()
    database.execute("PRAGMA foreign_keys=OFF")
    try:
        with database:
            database.execute("BEGIN")
            database.execute(replacement)
            columns = ",".join(
                '"' + str(row[1]) + '"'
                for row in database.execute("PRAGMA table_info(turns)")
            )
            database.execute(
                f"INSERT INTO turns_new ({columns}) SELECT {columns} FROM turns"
            )
            database.execute("DROP TABLE turns")
            database.execute("ALTER TABLE turns_new RENAME TO turns")
            for statement in objects:
                database.execute(statement)
            if database.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("foreign key violation after turns migration")
    finally:
        database.execute("PRAGMA foreign_keys=ON")


def _remove_goal_review_header(database: sqlite3.Connection) -> None:
    header = "[AUTONOMOUS GOAL REVIEW RECORD; not sent to the owner]\n"
    database.execute(
        """UPDATE messages SET content=substr(content, ?)
           WHERE role='assistant' AND delivery_state='internal'
             AND json_extract(source_event_ids_json, '$[0]')='goal-record:' || turn_id
             AND substr(content, 1, ?)=?""",
        (len(header) + 1, len(header), header),
    )


def _remove_heartbeat_record_header(database: sqlite3.Connection) -> None:
    header = "[AUTONOMOUS HEARTBEAT RECORD; not sent to the owner]\n"
    # Preserve the workflow identity of older records before removing the text
    # previously used to identify them.
    database.execute(
        """UPDATE turns SET workflow_kind='heartbeat'
           WHERE workflow_kind IS NULL AND EXISTS (
               SELECT 1 FROM messages m WHERE m.turn_id=turns.id
                 AND m.role='assistant' AND m.delivery_state='internal'
                 AND json_extract(m.source_event_ids_json, '$[0]')='heartbeat-record:' || m.turn_id
           )"""
    )
    database.execute(
        """UPDATE messages SET content=substr(content, ?)
           WHERE role='assistant' AND delivery_state='internal'
             AND json_extract(source_event_ids_json, '$[0]')='heartbeat-record:' || turn_id
             AND substr(content, 1, ?)=?""",
        (len(header) + 1, len(header), header),
    )


def _remove_obsolete_reply_context(database: sqlite3.Connection) -> None:
    obsolete = {
        "cooled_reply_expectation", "cooled_reply_source_turn_id",
        "cooled_reply_since", "cooled_reply_due_at", "cooled_reply_delay_minutes",
        "cooled_reply_waiting_since", "cooled_reply_review_at",
        "cooled_reply_checks", "cooled_reply_reason", "pending_reply_checks",
    }
    for column in sorted(_columns(database, "self_state") & obsolete):
        database.execute(f'ALTER TABLE self_state DROP COLUMN "{column}"')


MIGRATIONS: tuple[Migration, ...] = (
    _add_runtime_archive_metadata,
    _add_turn_workflow_kind,
    _add_memory_operation_workflow,
    _remove_goal_review_header,
    _remove_heartbeat_record_header,
    _remove_obsolete_reply_context,
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
