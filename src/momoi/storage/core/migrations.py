from __future__ import annotations

import sqlite3
import re
import json
import time
from collections.abc import Callable

from ..episode.episode_claims import render_verified_claims


from ..agenda.plans import PLAN_SCHEMA


def _add_task_plans(database):
    database.execute(PLAN_SCHEMA)


def _add_plan_context(database):
    if "context_json" not in _columns(database, "task_plans"):
        database.execute("ALTER TABLE task_plans ADD COLUMN context_json TEXT")


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


def _add_turn_workflow(database: sqlite3.Connection, workflow: str) -> None:
    sql = str(
        database.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='turns'"
        ).fetchone()[0]
    )
    if f"'{workflow}'" in sql:
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
        "'memory_maintenance'", f"'memory_maintenance', '{workflow}'"
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


def _add_memory_operation_workflow(database: sqlite3.Connection) -> None:
    _add_turn_workflow(database, "memory_operation")


def _add_plan_step_workflow(database: sqlite3.Connection) -> None:
    _add_turn_workflow(database, "plan_step")


def _add_current_state_workflow(database: sqlite3.Connection) -> None:
    _add_turn_workflow(database, "current_state_maintenance")


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


def _neutral_episode_speaker_metadata(database: sqlite3.Connection) -> None:
    database.execute(
        """UPDATE conversation_episodes
           SET emotional_context_json=json_remove(
               json_set(emotional_context_json, '$.assistant',
                   json_extract(emotional_context_json, '$.momoi')), '$.momoi')
           WHERE json_valid(emotional_context_json)
             AND json_type(emotional_context_json, '$.momoi') IS NOT NULL
             AND json_type(emotional_context_json, '$.assistant') IS NULL"""
    )
    # Rebuild generated framing from structured evidence, never replace text
    # inside titles, narratives, or source quotations.
    rows = database.execute(
        "SELECT id, working_summary_claims_json FROM conversation_episodes"
    ).fetchall()
    required = {"role", "delivery_state", "turn_id", "ordinal", "quote"}
    for episode_id, raw in rows:
        try:
            claims = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(claims, list) or not claims or not all(
            isinstance(claim, dict) and required <= claim.keys() for claim in claims
        ):
            continue
        database.execute(
            "UPDATE conversation_episodes SET working_summary=? WHERE id=?",
            (render_verified_claims(claims), episode_id),
        )


def _add_episode_recall_cues(database: sqlite3.Connection) -> None:
    if "recall_cues_json" not in _columns(database, "conversation_episodes"):
        database.execute(
            "ALTER TABLE conversation_episodes ADD COLUMN "
            "recall_cues_json TEXT NOT NULL DEFAULT '[]'"
        )
    # schema.sql recreates semantic_episodes_update on every open, including
    # its cue column dependency, before these additive migrations run.


def _add_episode_cue_vectors(database: sqlite3.Connection) -> None:
    # SQLite CHECK constraints require a table rebuild. Preserve vectors and
    # queue state, and recreate the original indexes on the replacement table.
    sql = database.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='semantic_documents'").fetchone()[0]
    if "'episode_cue'" in sql:
        return
    indexes = [row[0] for row in database.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='semantic_documents' AND sql IS NOT NULL")]
    replacement = sql.replace('semantic_documents', 'semantic_documents_with_cues', 1).replace("'episode_turn'", "'episode_turn', 'episode_cue'")
    database.execute(replacement)
    database.execute("INSERT INTO semantic_documents_with_cues SELECT * FROM semantic_documents")
    database.execute("DROP TABLE semantic_documents")
    database.execute("ALTER TABLE semantic_documents_with_cues RENAME TO semantic_documents")
    for index in indexes:
        database.execute(index)


def _restore_last_heartbeat_activity(database: sqlite3.Connection) -> None:
    # Owner end_turn used to overwrite these columns. Recover only the exact
    # heartbeat represented by the timestamp, never a deleted or older event.
    state = database.execute("SELECT last_heartbeat_at,activity_since FROM self_state WHERE id=1").fetchone()
    if state is None:
        return
    record = database.execute(
        """SELECT content,created_at FROM messages WHERE created_at=?
           AND delivery_state='internal'
           AND json_extract(source_event_ids_json,'$[0]')='heartbeat-record:' || turn_id
           ORDER BY id DESC LIMIT 1""", (state[0],),
    ).fetchone()
    activity = result = ""
    at = float(state[0] if state[0] is not None else state[1])
    if record and str(record[0]).startswith("Activity: "):
        body = str(record[0])[len("Activity: "):]
        if "\nResult: " in body:
            activity, result = body.split("\nResult: ", 1)
            if result == "(no concrete result recorded)":
                result = ""
    database.execute(
        "UPDATE self_state SET activity=?,activity_result=?,activity_since=? WHERE id=1",
        (activity, result, at),
    )


def _normalize_owner_message_received_at(database: sqlite3.Connection) -> None:
    """Restore owner text from its source events and use their reception time."""

    rows = database.execute(
        "SELECT id, source_event_ids_json FROM messages WHERE role='user'"
    ).fetchall()
    for message_id, source_json in rows:
        try:
            source_ids = json.loads(source_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(source_ids, list) or not source_ids or not all(
            isinstance(source_id, str) for source_id in source_ids
        ):
            continue
        placeholders = ",".join("?" for _ in source_ids)
        events = database.execute(
            f"SELECT id, content, received_at FROM events WHERE id IN ({placeholders})",
            tuple(source_ids),
        ).fetchall()
        by_id = {str(event[0]): event for event in events}
        if any(source_id not in by_id for source_id in source_ids):
            continue
        content = "\n".join(str(by_id[source_id][1]) for source_id in source_ids)
        received_at = min(float(by_id[source_id][2]) for source_id in source_ids)
        database.execute(
            "UPDATE messages SET content=?, created_at=? WHERE id=?",
            (content, received_at, message_id),
        )


def _drop_goal_authority(database: sqlite3.Connection) -> None:
    """Goals no longer split owner/agent authority; every Goal Turn runs trusted."""

    if "authority" not in _columns(database, "goals"):
        return
    objects = [
        row[0]
        for row in database.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='goals' "
            "AND type IN ('index','trigger') AND sql IS NOT NULL"
        )
    ]
    database.commit()
    database.execute("PRAGMA foreign_keys=OFF")
    try:
        with database:
            database.execute("BEGIN")
            database.execute(
                """CREATE TABLE goals_new (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    success_criteria TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('active', 'waiting', 'blocked', 'done', 'cancelled')
                    ),
                    plan_json TEXT NOT NULL,
                    next_action TEXT NOT NULL DEFAULT '',
                    waiting_for TEXT NOT NULL DEFAULT '',
                    blocked_reason TEXT NOT NULL DEFAULT '',
                    latest_result TEXT NOT NULL DEFAULT '',
                    schedule_json TEXT NOT NULL DEFAULT '',
                    next_review_at REAL,
                    retry_at REAL,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    review_claimed_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            columns = ",".join(
                '"' + str(row[1]) + '"'
                for row in database.execute("PRAGMA table_info(goals)")
                if str(row[1]) != "authority"
            )
            database.execute(
                f"INSERT INTO goals_new ({columns}) SELECT {columns} FROM goals"
            )
            database.execute("DROP TABLE goals")
            database.execute("ALTER TABLE goals_new RENAME TO goals")
            for statement in objects:
                database.execute(statement)
            if database.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("foreign key violation after goals migration")
    finally:
        database.execute("PRAGMA foreign_keys=ON")


def _add_memory_operation_failed_state(database: sqlite3.Connection) -> None:
    """Deterministically failing batches must stop retrying: add a failed state."""

    sql = str(
        database.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='memory_operation_batches'"
        ).fetchone()[0]
    )
    if "'failed'" in sql:
        return
    objects = [
        row[0]
        for row in database.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='memory_operation_batches' "
            "AND type IN ('index','trigger') AND sql IS NOT NULL"
        )
    ]
    database.commit()
    database.execute("PRAGMA foreign_keys=OFF")
    try:
        with database:
            database.execute("BEGIN")
            database.execute(
                """CREATE TABLE memory_operation_batches_new (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE REFERENCES turns(id),
                    state TEXT NOT NULL DEFAULT 'pending'
                        CHECK(state IN ('pending','running','completed','failed')),
                    operations_json TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    conversation_json TEXT NOT NULL,
                    events_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0,
                    result_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            columns = ",".join(
                '"' + str(row[1]) + '"'
                for row in database.execute(
                    "PRAGMA table_info(memory_operation_batches)"
                )
            )
            database.execute(
                f"INSERT INTO memory_operation_batches_new ({columns}) "
                f"SELECT {columns} FROM memory_operation_batches"
            )
            database.execute("DROP TABLE memory_operation_batches")
            database.execute(
                "ALTER TABLE memory_operation_batches_new "
                "RENAME TO memory_operation_batches"
            )
            for statement in objects:
                database.execute(statement)
            database.execute(
                "UPDATE sqlite_sequence SET seq=("
                "SELECT COALESCE(MAX(sequence), 0) FROM memory_operation_batches"
                ") WHERE name='memory_operation_batches'"
            )
            if database.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError(
                    "foreign key violation after memory_operation_batches migration"
                )
    finally:
        database.execute("PRAGMA foreign_keys=ON")


def _add_memory_operation_failures(database: sqlite3.Connection) -> None:
    """Separate "how often claimed" from "how often it actually failed".

    `attempts` counts every claim, and an interrupted claim (process restart,
    cancellation) is not a failure. Gating the retry bound on it let repeated
    interruptions consume the failure budget, so a batch could be killed by its
    first genuine error, and an interrupted batch was re-claimed without bound.
    `failures` counts only releases that reported a real error.
    """
    if "failures" not in _columns(database, "memory_operation_batches"):
        database.execute(
            "ALTER TABLE memory_operation_batches "
            "ADD COLUMN failures INTEGER NOT NULL DEFAULT 0"
        )


def _unify_memory_kinds(database: sqlite3.Connection) -> None:
    """Put both memory planes on one kind vocabulary.

    Renames merge into the surviving kind. Kinds the vocabulary drops are
    tombstoned rather than deleted, matching the episodic retirement: the rows
    stay as history and are invisible everywhere. `shared` and
    `shared_experience` go because episodes narrate one-off events.
    """

    database.execute(
        """CREATE TABLE IF NOT EXISTS reflection_memory_tombstones (
            kind TEXT NOT NULL,
            key TEXT NOT NULL,
            evidence_quote TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (kind, key)
        )"""
    )
    now = time.time()
    for old, new in (
        ("routine", "profile"),
        ("owner_profile", "profile"),
        ("owner_preference", "preference"),
        ("tool_skill", "practice"),
    ):
        # A legacy alias may collide with an already-existing row under the
        # canonical kind. Preserve the legacy row as hidden history instead of
        # letting the UNIQUE(kind, key) constraint abort the whole migration.
        for row in database.execute(
            """SELECT kind, key, source_event_id, evidence_quote FROM memories
               WHERE kind=? AND EXISTS (
                   SELECT 1 FROM memories AS target
                   WHERE target.kind=? AND target.key=memories.key
               )""",
            (old, new),
        ).fetchall():
            database.execute(
                """INSERT INTO memory_tombstones
                   (kind, key, source_event_id, evidence_quote, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(kind, key) DO UPDATE SET
                     source_event_id=excluded.source_event_id,
                     evidence_quote=excluded.evidence_quote,
                     created_at=excluded.created_at""",
                (row["kind"], row["key"], row["source_event_id"],
                 row["evidence_quote"], now),
            )
        for row in database.execute(
            """SELECT kind, key, evidence FROM reflection_memories
               WHERE kind=? AND EXISTS (
                   SELECT 1 FROM reflection_memories AS target
                   WHERE target.kind=? AND target.key=reflection_memories.key
               )""",
            (old, new),
        ).fetchall():
            database.execute(
                """INSERT INTO reflection_memory_tombstones
                   (kind, key, evidence_quote, created_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(kind, key) DO UPDATE SET
                     evidence_quote=excluded.evidence_quote,
                     created_at=excluded.created_at""",
                (row["kind"], row["key"], str(row["evidence"])[:500], now),
            )
        database.execute(
            """UPDATE memories SET kind=? WHERE kind=?
               AND NOT EXISTS (
                   SELECT 1 FROM memories AS target
                   WHERE target.kind=? AND target.key=memories.key
               )""",
            (new, old, new),
        )
        database.execute(
            """UPDATE reflection_memories SET kind=? WHERE kind=?
               AND NOT EXISTS (
                   SELECT 1 FROM reflection_memories AS target
                   WHERE target.kind=? AND target.key=reflection_memories.key
               )""",
            (new, old, new),
        )

    # `shared` facts are episode material now: retire the confirmed ones the way
    # the episodic retirement did, and tombstone the reflection ones.
    for kind, key, source_event_id, evidence_quote in database.execute(
        """SELECT kind, key, source_event_id, evidence_quote FROM memories
           WHERE superseded_by IS NULL AND kind='shared'"""
    ).fetchall():
        database.execute(
            """INSERT INTO memory_tombstones
               (kind, key, source_event_id, evidence_quote, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(kind, key) DO UPDATE SET
                 source_event_id=excluded.source_event_id,
                 evidence_quote=excluded.evidence_quote,
                 created_at=excluded.created_at""",
            (kind, key, source_event_id, evidence_quote, now),
        )
    for kind, key, evidence in database.execute(
        """SELECT kind, key, evidence FROM reflection_memories
           WHERE kind='shared_experience'"""
    ).fetchall():
        database.execute(
            """INSERT INTO reflection_memory_tombstones
               (kind, key, evidence_quote, created_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(kind, key) DO UPDATE SET
                 evidence_quote=excluded.evidence_quote,
                 created_at=excluded.created_at""",
            (kind, key, str(evidence)[:500], now),
        )


def _retire_episodic_and_recent_memories(database: sqlite3.Connection) -> None:
    """Retire the episodic kind and the recent activation.

    Episodes narrate one-off events and current state owns temporary state;
    memories keep only durable rules, preferences, relationships, procedures,
    and cross-event states. Tombstoned rows stay invisible everywhere and the
    semantic triggers reindex them out.
    """

    rows = database.execute(
        """SELECT kind, key, source_event_id, evidence_quote FROM memories
           WHERE superseded_by IS NULL AND (kind='episodic' OR activation='recent')"""
    ).fetchall()
    for kind, key, source_event_id, evidence_quote in rows:
        database.execute(
            """INSERT INTO memory_tombstones
               (kind, key, source_event_id, evidence_quote, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(kind, key) DO UPDATE SET
                 source_event_id=excluded.source_event_id,
                 evidence_quote=excluded.evidence_quote,
                 created_at=excluded.created_at""",
            (kind, key, source_event_id, evidence_quote, time.time()),
        )


MIGRATIONS: tuple[Migration, ...] = (
    _add_runtime_archive_metadata,
    _add_turn_workflow_kind,
    _add_memory_operation_workflow,
    _remove_goal_review_header,
    _remove_heartbeat_record_header,
    _remove_obsolete_reply_context,
    _neutral_episode_speaker_metadata,
    _add_episode_recall_cues,
    _add_episode_cue_vectors,
    _restore_last_heartbeat_activity,
    _add_current_state_workflow,
    _normalize_owner_message_received_at,
    _drop_goal_authority,
    _add_memory_operation_failed_state,
    _retire_episodic_and_recent_memories,
    _add_memory_operation_failures,
    _unify_memory_kinds,
    _add_plan_step_workflow,
    _add_task_plans,
    _add_plan_context,
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
