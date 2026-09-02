import time

from .memory_values import memory_snapshot_fingerprint


class MemoryMaintenanceCommitStore:
    def apply_memory_maintenance_batch(
        self,
        turn_id: str,
        decision: dict[str, object],
        mutable_memories: dict[int, dict[str, object]],
        *,
        owner_marker: tuple[float, str],
    ) -> None:
        now = time.time()
        with self._db:
            if self.latest_owner_event_marker() != owner_marker:
                raise ValueError("owner_evidence_changed")
            current: dict[int, dict[str, object]] = {}
            for memory_id, snapshot in mutable_memories.items():
                row = self._db.execute(
                    """SELECT id, kind, key, content, activation, authority,
                              source_event_id, evidence_quote, importance,
                              created_at, updated_at, expires_at, superseded_by
                       FROM memories AS m
                       WHERE m.id=? AND m.superseded_by IS NULL
                         AND (m.expires_at IS NULL OR m.expires_at>?)
                         AND NOT EXISTS (
                           SELECT 1 FROM memory_tombstones AS t
                           WHERE t.kind=m.kind AND t.key=m.key
                         )""",
                    (memory_id, now),
                ).fetchone()
                if row is None:
                    raise ValueError("memory_snapshot_changed")
                item = dict(row)
                if memory_snapshot_fingerprint(item) != memory_snapshot_fingerprint(
                    snapshot
                ):
                    raise ValueError("memory_snapshot_changed")
                current[memory_id] = item

            for change in decision.get("changes", []):
                if not isinstance(change, dict):
                    raise ValueError("invalid_memory_maintenance_change")
                action = str(change["action"])
                if action == "replace":
                    memory_id = int(change["memory_id"])
                    row = current[memory_id]
                    activation = str(change["activation"])
                    expires_at = change.get("expires_at")
                    if row["activation"] != "always" and activation == "always":
                        raise ValueError("memory_maintenance_promotes_always")
                    if activation == "recent":
                        if (
                            isinstance(expires_at, bool)
                            or not isinstance(expires_at, (int, float))
                            or not now < float(expires_at) <= now + 7 * 86400
                        ):
                            raise ValueError("invalid_memory_maintenance_expiry")
                    elif expires_at is not None:
                        raise ValueError("invalid_memory_maintenance_expiry")
                    evidence = change.get("evidence")
                    if isinstance(evidence, dict):
                        event_id = str(evidence["event_id"])
                        quote = str(evidence["quote"])
                        event = self._db.execute(
                            "SELECT content, occurred_at FROM events WHERE id=?",
                            (event_id,),
                        ).fetchone()
                        if event is None or quote not in str(event["content"]):
                            raise ValueError("invalid_memory_maintenance_evidence")
                        source_event_id = event_id
                        evidence_quote = quote
                        updated_at = float(event["occurred_at"])
                    else:
                        source_event_id = str(row["source_event_id"])
                        evidence_quote = str(row["evidence_quote"])
                        updated_at = float(row["updated_at"])
                    self._db.execute(
                        """UPDATE memories SET content=?, activation=?,
                           expires_at=?, source_event_id=?, evidence_quote=?,
                           updated_at=? WHERE id=? AND superseded_by IS NULL""",
                        (
                            str(change["content"]),
                            activation,
                            expires_at,
                            source_event_id,
                            evidence_quote,
                            updated_at,
                            memory_id,
                        ),
                    )
                    if isinstance(evidence, dict):
                        self._add_memory_evidence(
                            memory_id, source_event_id, evidence_quote, updated_at
                        )
                elif action == "merge":
                    survivor_id = int(change["survivor_id"])
                    source_ids = [int(item) for item in change["source_ids"]]
                    survivor = current[survivor_id]
                    activation = str(change["activation"])
                    expires_at = change.get("expires_at")
                    if survivor["activation"] != "always" and activation == "always":
                        raise ValueError("memory_maintenance_promotes_always")
                    if activation == "recent":
                        if (
                            isinstance(expires_at, bool)
                            or not isinstance(expires_at, (int, float))
                            or not now < float(expires_at) <= now + 7 * 86400
                        ):
                            raise ValueError("invalid_memory_maintenance_expiry")
                    elif expires_at is not None:
                        raise ValueError("invalid_memory_maintenance_expiry")
                    evidence_event_ids = [
                        str(event_id) for event_id in change["evidence_event_ids"]
                    ]
                    placeholders = ",".join("?" for _ in evidence_event_ids)
                    cited_events = self._db.execute(
                        f"""SELECT id,content,occurred_at FROM events
                            WHERE id IN ({placeholders})""",
                        evidence_event_ids,
                    ).fetchall()
                    if len(cited_events) != len(evidence_event_ids):
                        raise ValueError("invalid_memory_maintenance_evidence")
                    newest_event = max(
                        cited_events, key=lambda item: float(item["occurred_at"])
                    )
                    for source_id in source_ids:
                        self._db.execute(
                            """INSERT OR IGNORE INTO memory_evidence
                               (memory_id, source_event_id, quote, created_at)
                               SELECT ?, source_event_id, quote, created_at
                               FROM memory_evidence WHERE memory_id=?""",
                            (survivor_id, source_id),
                        )
                        self._db.execute(
                            """UPDATE memories SET superseded_by=?
                               WHERE id=? AND superseded_by IS NULL""",
                            (survivor_id, source_id),
                        )
                    for event in cited_events:
                        self._add_memory_evidence(
                            survivor_id,
                            str(event["id"]),
                            str(event["content"]),
                            float(event["occurred_at"]),
                        )
                    self._db.execute(
                        """UPDATE memories SET content=?, activation=?, expires_at=?,
                           source_event_id=?, evidence_quote=?, updated_at=?
                           WHERE id=? AND superseded_by IS NULL""",
                        (
                            str(change["content"]),
                            activation,
                            expires_at,
                            newest_event["id"],
                            newest_event["content"],
                            newest_event["occurred_at"],
                            survivor_id,
                        ),
                    )
                elif action == "retire":
                    memory_id = int(change["memory_id"])
                    row = current[memory_id]
                    evidence = change["evidence"]
                    assert isinstance(evidence, dict)
                    event_id = str(evidence["event_id"])
                    quote = str(evidence["quote"])
                    event = self._db.execute(
                        "SELECT content FROM events WHERE id=?", (event_id,)
                    ).fetchone()
                    if event is None or quote not in str(event["content"]):
                        raise ValueError("invalid_memory_maintenance_evidence")
                    sibling = self._db.execute(
                        """SELECT 1 FROM memories
                           WHERE kind=? AND key=? AND id<>?
                             AND superseded_by IS NULL LIMIT 1""",
                        (row["kind"], row["key"], memory_id),
                    ).fetchone()
                    if sibling is not None:
                        raise ValueError("memory_maintenance_tombstone_conflict")
                    self._db.execute(
                        """INSERT INTO memory_tombstones
                           (kind, key, source_event_id, evidence_quote, created_at)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(kind,key) DO UPDATE SET
                             source_event_id=excluded.source_event_id,
                             evidence_quote=excluded.evidence_quote,
                             created_at=excluded.created_at""",
                        (row["kind"], row["key"], event_id, quote, now),
                    )
                else:
                    raise ValueError("invalid_memory_maintenance_change")

            self._append_turn_journal(
                turn_id,
                "memory_maintenance_batch",
                {
                    "reviewed_ids": list(decision.get("reviewed_ids", [])),
                    "completed_ids": list(decision.get("completed_ids", [])),
                    "change_count": len(decision.get("changes", [])),
                    "regroup_requests": list(decision.get("regroup_requests", [])),
                    "summary": str(decision.get("summary") or ""),
                    "owner_marker": [owner_marker[0], owner_marker[1]],
                },
                visibility="internal",
                trust="runtime",
                created_at=now,
            )
