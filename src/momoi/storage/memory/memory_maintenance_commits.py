import time

from ...memory.storage.transactions import transaction


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
        with transaction(self._db):
            if self.latest_owner_event_marker() != owner_marker:
                raise ValueError("owner_evidence_changed")
            current = self.memory.validate_snapshots(mutable_memories)

            created_ids = []
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
                    if expires_at is not None:
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
                    replacement_id = self.memory.replace(
                        memory_id, str(change["content"]), activation, expires_at,
                        {"event_id": source_event_id, "quote": evidence_quote},
                        updated_at=updated_at,
                    )
                    created_ids.append(replacement_id)
                elif action == "merge":
                    survivor_id = int(change["survivor_id"])
                    source_ids = [int(item) for item in change["source_ids"]]
                    survivor = current[survivor_id]
                    activation = str(change["activation"])
                    expires_at = change.get("expires_at")
                    if survivor["activation"] != "always" and activation == "always":
                        raise ValueError("memory_maintenance_promotes_always")
                    if expires_at is not None:
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
                    replacement_id = self.memory.merge(
                        survivor_id, source_ids, str(change["content"]),
                        activation, expires_at, cited_events,
                    )
                    created_ids.append(replacement_id)
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
                    self.memory.forget(
                        row, {"event_id": event_id, "quote": quote},
                        now=now, require_unique=True,
                    )
                else:
                    raise ValueError("invalid_memory_maintenance_change")

            self._append_turn_journal(
                turn_id,
                "memory_maintenance_batch",
                {
                    "reviewed_ids": list(decision.get("reviewed_ids", [])),
                    "completed_ids": list(dict.fromkeys([*decision.get("completed_ids", []), *created_ids])),
                    "created_ids": created_ids,
                    "change_count": len(decision.get("changes", [])),
                    "regroup_requests": list(decision.get("regroup_requests", [])),
                    "summary": str(decision.get("summary") or ""),
                    "owner_marker": [owner_marker[0], owner_marker[1]],
                },
                visibility="internal",
                trust="runtime",
                created_at=now,
            )
