#!/usr/bin/env python3
"""Preview or clear history after a retained message, without printing content.

Run on the Docker host:
  python3 clear_chat_after.py --workspace /path/to/momoi --anchor-id 17610
  python3 clear_chat_after.py --workspace /path/to/momoi --anchor-id 17610 --container momoi --apply

Apply stops and restarts the container. The anchor is retained by message ID,
including when later bubbles share its timestamp. Preview rolls back all SQL.
Only IDs, counts and timestamps are printed. No content backup is created.
"""

import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import unicodedata


def workspace_paths(root):
    config = json.loads((root / "config.json").read_text())
    storage = config.get("storage", {})
    database = (root / storage.get("database", "data/momoi.sqlite3")).resolve()
    thinking = (root / storage.get("thinking", "data/thinking")).resolve()
    if not database.is_relative_to(root) or not thinking.is_relative_to(root):
        raise ValueError("storage_outside_workspace")
    if not database.is_file():
        raise ValueError("database_missing")
    return database, thinking


def clear_after(root, anchor_id, *, apply=False):
    database, thinking_root = workspace_paths(root)
    thinking = sorted(thinking_root.glob("thinking-*.sqlite3"))
    if any(p.is_symlink() for p in thinking):
        raise ValueError("symlinked_thinking_database")
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    counts = {}
    files = {}
    try:
        connection.execute("PRAGMA secure_delete=ON")
        for i, path in enumerate(thinking):
            connection.execute(f"ATTACH DATABASE ? AS think{i}", (str(path),))
            connection.execute(f"PRAGMA think{i}.secure_delete=ON")
        connection.execute("BEGIN IMMEDIATE")
        anchor = connection.execute(
            "SELECT id,turn_id,created_at,outbox_id FROM messages WHERE id=?", (anchor_id,),
        ).fetchone()
        if anchor is None or not anchor["turn_id"]:
            raise ValueError("anchor_missing_or_without_turn")
        cut = anchor["created_at"]
        # Keep state values inside this process; the report contains only time.
        mood = connection.execute(
            """SELECT created_at,json_extract(payload_json,'$.mood_change') value
               FROM turn_journal WHERE item_type='final' AND created_at<=?
                 AND json_type(payload_json,'$.mood_change')='object'
               ORDER BY created_at DESC LIMIT 1""", (cut,),
        ).fetchone()
        current = connection.execute(
            "SELECT mood_state,mood_intensity,mood_cause,mood_updated_at FROM self_state WHERE id=1"
        ).fetchone()
        if current and current["mood_updated_at"] <= cut and (
            mood is None or current["mood_updated_at"] >= mood["created_at"]
        ):
            mood_at = current["mood_updated_at"]
            mood_value = dict(zip(("state", "intensity", "cause"), tuple(current)[:3]))
        elif mood is not None:
            mood_at = mood["created_at"]
            mood_value = json.loads(mood["value"])
        else:
            raise ValueError("historical_mood_unavailable")
        if not {"state", "intensity", "cause"} <= mood_value.keys():
            raise ValueError("historical_mood_incomplete")
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        connection.execute("CREATE TEMP TABLE selected(id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO selected SELECT id FROM turns WHERE started_at>? AND id<>?", (cut, anchor["turn_id"]))
        connection.execute("INSERT OR IGNORE INTO selected SELECT DISTINCT turn_id FROM messages WHERE id>? AND turn_id<>?", (anchor_id, anchor["turn_id"]))
        connection.execute("CREATE TEMP TABLE affected(id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO affected SELECT DISTINCT episode_id FROM episode_turns WHERE turn_id IN(SELECT id FROM selected) OR turn_id=?", (anchor["turn_id"],))
        connection.execute("CREATE TEMP TABLE selected_events(id TEXT PRIMARY KEY)")
        connection.execute("""INSERT OR IGNORE INTO selected_events SELECT j.value FROM messages m,json_each(m.source_event_ids_json) j
            WHERE m.turn_id IN(SELECT id FROM selected)""")
        connection.execute("INSERT OR IGNORE INTO selected_events SELECT id FROM events WHERE occurred_at>?", (cut,))
        # Shared blobs must remain while another manifest references them.
        connection.execute("CREATE TEMP TABLE selected_blobs(id TEXT PRIMARY KEY)")
        blob_columns = ("system_sha256", "messages_sha256", "tools_sha256", "payload_sha256")
        if "context_manifests" in tables:
            for column in blob_columns:
                connection.execute(f"INSERT OR IGNORE INTO selected_blobs SELECT {column} FROM context_manifests WHERE turn_id IN(SELECT id FROM selected) OR turn_id=?", (anchor["turn_id"],))

        def delete(table, predicate, parameters=()):
            if table not in tables and not table.startswith("think"):
                return
            counts[table] = connection.execute(f"DELETE FROM {table} WHERE {predicate}", parameters).rowcount

        for i in range(len(thinking)):
            delete(f"think{i}.calls", "created_at>? OR turn_id IN(SELECT id FROM selected)", (cut,))
        delete("semantic_documents", "parent_id IN(SELECT id FROM affected) OR (document_type='episode_summary' AND source_id IN(SELECT id FROM affected))")
        # Select actual outbox IDs, never rely on a nullable anchor outbox ID.
        delete("outbox", "id IN(SELECT outbox_id FROM messages WHERE id>?) OR turn_id IN(SELECT id FROM selected)", (anchor_id,))
        for table in ("tool_audit", "turn_progress", "reconciliations", "context_manifests", "context_plans", "notifications", "turn_journal"):
            delete(table, "turn_id IN(SELECT id FROM selected) OR turn_id=?", (anchor["turn_id"],))
        delete("messages", "id>?", (anchor_id,))
        delete("events", "id IN(SELECT id FROM selected_events)")
        delete("turns", "id IN(SELECT id FROM selected)")
        delete("conversation_episodes", "id IN(SELECT id FROM affected) AND NOT EXISTS(SELECT 1 FROM episode_turns et WHERE et.episode_id=conversation_episodes.id)")
        counts["summaries_invalidated"] = connection.execute("""UPDATE conversation_episodes SET
            working_summary='',working_summary_claims_json='[]',narrative_summary='',summary='',
            emotional_context_json='{}',outcomes_json='[]',summarized_through_ordinal=0,
            summary_claimed_at=NULL,summary_retry_at=NULL,summary_failure_count=0,summary_abandoned_at=NULL,
            updated_at=COALESCE((SELECT MAX(m.created_at) FROM episode_turns et JOIN messages m ON m.turn_id=et.turn_id WHERE et.episode_id=conversation_episodes.id),created_at)
            WHERE id IN(SELECT id FROM affected)""").rowcount
        for row in connection.execute("SELECT id,title,topics_json,entities_json,open_loops_json FROM conversation_episodes WHERE id IN(SELECT id FROM affected)").fetchall():
            key = connection.execute("SELECT id FROM recall_episode_ids WHERE episode_id=?", (row["id"],)).fetchone()
            if key is None:
                continue
            key = key[0]
            connection.execute("DELETE FROM episode_recall_terms WHERE episode_key=?", (key,))
            connection.execute("INSERT OR IGNORE INTO episode_recall_terms SELECT episode_key,term_id FROM episode_message_recall_terms WHERE episode_key=?", (key,))
            values = [row["title"]]
            for field in ("topics_json", "entities_json", "open_loops_json"):
                values.extend(json.loads(row[field]))
            for value in values:
                for part in str(value).replace("｜", "|").split("|")[:12]:
                    term = unicodedata.normalize("NFKC", part).casefold().strip()
                    if not term:
                        continue
                    connection.execute("INSERT OR IGNORE INTO recall_terms(term) VALUES (?)", (term,))
                    connection.execute("INSERT OR IGNORE INTO episode_recall_terms SELECT ?,id FROM recall_terms WHERE term=?", (key, term))
        delete("semantic_dirty_sources", "source_type='episode' AND source_id IN(SELECT id FROM affected) AND source_id NOT IN(SELECT id FROM conversation_episodes)")
        if "context_blobs" in tables and "context_manifests" in tables:
            refs = " UNION ".join(f"SELECT {col} FROM context_manifests WHERE {col} IS NOT NULL" for col in blob_columns)
            delete("context_blobs", f"sha256 IN(SELECT id FROM selected_blobs) AND sha256 NOT IN({refs})")
        connection.execute("""UPDATE self_state SET mood_state=?,mood_intensity=?,mood_cause=?,mood_updated_at=?,
            pending_reply_turn_id=NULL,pending_reply_expectation='',pending_reply_since=NULL,
            pending_reply_channel='',pending_reply_next_check_at=NULL,pending_reply_last_reason='',
            pending_reply_delay_minutes=0 WHERE id=1""", (mood_value["state"], mood_value["intensity"], mood_value["cause"], mood_at))
        connection.execute("UPDATE transcript_window_state SET observed_turn_id='',observed_updated_at=0")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("foreign_key_check_failed")
        if connection.execute("SELECT COUNT(*) FROM messages WHERE id>?", (anchor_id,)).fetchone()[0]:
            raise ValueError("later_messages_remain")
        if connection.execute("SELECT COUNT(*) FROM messages WHERE id=?", (anchor_id,)).fetchone()[0] != 1:
            raise ValueError("anchor_not_preserved")
        for folder, label in ((root / "llm-dumps", "llm_dumps"), (database.parent / "tool-results", "tool_results")):
            files[label] = [p for p in folder.glob("*.json") if not p.is_symlink() and p.stat().st_mtime >= cut]
        report = {"applied": apply, "anchor_id": anchor_id, "anchor_at": cut, "mood_source_at": mood_at,
                  "counts": counts, "files": {key: len(value) for key, value in files.items()}}
        if apply:
            connection.commit()
        else:
            connection.rollback()
    finally:
        connection.close()
    if apply:
        for paths in files.values():
            for path in paths:
                path.unlink()
        for path in [database, *thinking]:
            with sqlite3.connect(path, timeout=30) as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--anchor-id", required=True, type=int)
    parser.add_argument("--container", help="Required with --apply; must mount this workspace")
    parser.add_argument("--apply", action="store_true", help="Delete; omitted means transactional preview")
    args = parser.parse_args()
    root = args.workspace.resolve()
    restart = False
    try:
        if args.apply:
            if not args.container:
                raise ValueError("apply_requires_container")
            item = json.loads(subprocess.check_output(["docker", "inspect", args.container], stderr=subprocess.DEVNULL))[0]
            if not any(Path(m["Source"]).resolve() == root for m in item["Mounts"]):
                raise ValueError("container_workspace_mismatch")
            restart = item["State"]["Running"]
            if restart:
                subprocess.run(["docker", "stop", "-t", "30", args.container], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        report = clear_after(root, args.anchor_id, apply=args.apply)
    finally:
        if restart:
            subprocess.run(["docker", "start", args.container], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Exceptions must not expose SQL values or file contents.
        print(json.dumps({"error_type": type(error).__name__, "completed": False}))
        raise SystemExit(1) from None
