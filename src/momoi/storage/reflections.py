import json
import time
from datetime import datetime, timedelta

from ..config import ReflectionConfig
from .integrity import decode_stored_json
from .memory_values import estimate_tokens, truncate_tokens
from .timestamps import add_context_timestamps

REFLECTION_MEMORY_KINDS = {
    "owner_profile",
    "owner_preference",
    "world_knowledge",
    "self_insight",
    "relationship",
    "shared_experience",
    "practice",
    "tool_skill",
}


def _reflection_json(
    value: object,
    fallback: list[object] | dict[str, object],
    *,
    record_id: object,
    field: str,
) -> list[object] | dict[str, object]:
    return decode_stored_json(
        value,
        entity="reflection_material",
        record_id=record_id,
        field=field,
        expected_type=type(fallback),
        fallback=fallback,
    )

def _reflection_compact_value(value: object, limit: int = 240) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return " ".join(str(value or "").split())[:limit]

def _reflection_select_entries(
    entries: list[tuple[float, str, str, bool, bool]], token_budget: int
) -> list[tuple[float, str, str, bool, bool]]:
    """Keep a day-wide shape instead of selecting only the latest records."""
    if not entries or token_budget <= 0:
        return []
    ordered = sorted(entries)
    total = sum(estimate_tokens(f"[{label}]\n{content}") for _, label, content, _, _ in ordered)
    if total <= token_budget:
        return ordered
    selected: list[tuple[float, str, str, bool, bool]] = []
    selected_ids: set[int] = set()
    used = 0

    def add(index: int) -> None:
        nonlocal used
        if index in selected_ids:
            return
        entry = ordered[index]
        size = estimate_tokens(f"[{entry[1]}]\n{entry[2]}")
        if used + size > token_budget:
            return
        selected_ids.add(index)
        selected.append(entry)
        used += size

    head = max(1, len(ordered) // 5)
    tail = max(1, len(ordered) // 2)
    for index in range(head):
        add(index)
    for index in range(max(0, len(ordered) - tail), len(ordered)):
        add(index)
    for index, entry in enumerate(ordered):
        if entry[1] in {"OWNER", "EVENT", "RUNTIME FAILURE"} or entry[1].startswith("TOOL "):
            add(index)
    if not selected:
        entry = ordered[-1]
        selected = [
            (*entry[:2], truncate_tokens(entry[2], max(1, token_budget - 4)), *entry[3:])
        ]
    return sorted(selected)


class ReflectionStore:
    def _reflection_slot(
        self, now: float, at: str
    ) -> tuple[str, float, datetime]:
        local = datetime.fromtimestamp(now, self._timezone)
        hour, minute = map(int, at.split(":"))
        scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scheduled.timestamp() > now:
            scheduled -= timedelta(days=1)
        local_date = (scheduled.date() - timedelta(days=1)).isoformat()
        return local_date, scheduled.timestamp(), scheduled

    def claim_manual_reflection(
        self,
        now: float | None = None,
    ) -> dict[str, object] | None:
        now = time.time() if now is None else now
        local_date = datetime.fromtimestamp(now, self._timezone).date().isoformat()
        reflection_id = f"reflection:{local_date}"
        with self._db:
            self._db.execute(
                """INSERT OR IGNORE INTO reflections
                   (id, local_date, state, scheduled_at, created_at)
                   VALUES (?, ?, 'pending', ?, ?)""",
                (reflection_id, local_date, now, now),
            )
            row = self._db.execute(
                "SELECT * FROM reflections WHERE id=?",
                (reflection_id,),
            ).fetchone()
            if row is None or row["state"] == "running":
                return None
            self._db.execute(
                """UPDATE reflections SET state='running', claimed_at=?,
                   retry_at=NULL, error=NULL WHERE id=?""",
                (now, reflection_id),
            )
            claimed = self._db.execute(
                "SELECT * FROM reflections WHERE id=?",
                (reflection_id,),
            ).fetchone()
        return dict(claimed) if claimed is not None else None

    def claim_due_reflection(
        self,
        config: ReflectionConfig,
        now: float | None = None,
    ) -> dict[str, object] | None:
        if not config.enabled:
            return None
        now = time.time() if now is None else now
        local_date, scheduled_at, _ = self._reflection_slot(now, config.at)
        reflection_id = f"reflection:{local_date}"
        with self._db:
            self._db.execute(
                """INSERT OR IGNORE INTO reflections
                   (id, local_date, state, scheduled_at, created_at)
                   VALUES (?, ?, 'pending', ?, ?)""",
                (reflection_id, local_date, scheduled_at, now),
            )
            row = self._db.execute(
                """SELECT * FROM reflections
                   WHERE id=? AND state='pending' AND claimed_at IS NULL
                     AND scheduled_at<=? AND COALESCE(retry_at, 0)<=?""",
                (reflection_id, now, now),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                """UPDATE reflections SET state='running', claimed_at=?, error=NULL
                   WHERE id=?""",
                (now, reflection_id),
            )
        return dict(row)

    def next_reflection_due_at(
        self,
        config: ReflectionConfig,
        now: float | None = None,
    ) -> float | None:
        if not config.enabled:
            return None
        now = time.time() if now is None else now
        local_date, scheduled_at, scheduled = self._reflection_slot(
            now, config.at
        )
        row = self._db.execute(
            "SELECT state, retry_at FROM reflections WHERE local_date=?",
            (local_date,),
        ).fetchone()
        if row is None:
            return scheduled_at
        if row["state"] == "pending":
            return max(scheduled_at, float(row["retry_at"] or 0))
        if row["state"] == "running":
            return None
        next_scheduled = scheduled + timedelta(days=1)
        return next_scheduled.timestamp()

    def release_reflection(
        self, local_date: str, error: str, delay_seconds: float = 300
    ) -> None:
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE reflections SET state='pending', claimed_at=NULL,
                   retry_at=?, error=? WHERE local_date=? AND state='running'""",
                (now + delay_seconds, error[:500], local_date),
            )

    def restore_completed_reflection_claim(self, local_date: str) -> None:
        with self._db:
            self._db.execute(
                """UPDATE reflections SET state='completed', claimed_at=NULL,
                   retry_at=NULL, error=NULL
                   WHERE local_date=? AND state='running'""",
                (local_date,),
            )

    def reflection_source(
        self, local_date: str, token_budget: int
    ) -> dict[str, object]:
        start = datetime.fromisoformat(f"{local_date}T00:00:00").replace(
            tzinfo=self._timezone
        )
        end = start + timedelta(days=1)
        entries: list[tuple[float, str, str, bool, bool]] = []
        for row in self._db.execute(
            """SELECT role, content, created_at, delivery_state FROM messages
               WHERE created_at>=? AND created_at<?
                 AND (role IN ('user', 'event') OR delivery_state IN
                      ('delivered', 'uncertain', 'internal'))
               ORDER BY created_at""",
            (start.timestamp(), end.timestamp()),
        ).fetchall():
            owner = row["role"] == "user"
            if owner:
                label = "OWNER"
            elif row["role"] == "event":
                label = "EVENT"
            else:
                label = "MOMOI"
            if row["role"] != "event" and row["delivery_state"] == "internal":
                label = "MOMOI INTERNAL (not sent to owner)"
            elif row["role"] != "event" and row["delivery_state"] == "uncertain":
                label = "MOMOI DELIVERY UNCERTAIN"
            entries.append(
                (
                    float(row["created_at"]),
                    label,
                    str(row["content"]),
                    owner,
                    owner,
                )
            )
        for row in self._db.execute(
            """SELECT a.tool_name, a.state, a.ok, a.capability, t.started_at
               FROM tool_audit AS a JOIN turns AS t ON t.id=a.turn_id
               WHERE t.started_at>=? AND t.started_at<? ORDER BY t.started_at""",
            (start.timestamp(), end.timestamp()),
        ).fetchall():
            ok = "unknown" if row["ok"] is None else str(bool(row["ok"])).lower()
            entries.append(
                (
                    float(row["started_at"]),
                    f"TOOL {row['tool_name']}",
                    f"state={row['state']} ok={ok} capability={row['capability']}",
                    False,
                    False,
                )
            )
        for row in self._db.execute(
            """SELECT failure_reason, started_at FROM turns
               WHERE started_at>=? AND started_at<? AND failure_reason IS NOT NULL""",
            (start.timestamp(), end.timestamp()),
        ).fetchall():
            entries.append(
                (
                    float(row["started_at"]),
                    "RUNTIME FAILURE",
                    str(row["failure_reason"]),
                    False,
                    False,
                )
            )
        selected = _reflection_select_entries(entries, token_budget)
        text = "\n\n".join(
            f"[{self.context_timestamp(occurred_at)} {label}]\n{content}"
            for occurred_at, label, content, _, _ in selected
        )
        owner_text = "\n".join(content for _, _, content, owner, _ in selected if owner)
        knowledge_text = "\n".join(
            content for _, _, content, _, knowledge in selected if knowledge
        )

        mood_entries: list[str] = []
        mutation_entries: list[str] = []
        tool_calls: dict[tuple[str, str], dict[str, object]] = {}
        tool_order: list[tuple[str, str]] = []
        for row in self._db.execute(
            """SELECT j.turn_id, j.created_at, j.item_type, j.trust,
                      j.payload_json
               FROM turn_journal AS j JOIN turns AS t ON t.id=j.turn_id
               WHERE j.created_at>=? AND j.created_at<?
                 AND j.item_type IN ('final','tool_call','tool_result')
               ORDER BY j.created_at, j.sequence""",
            (start.timestamp(), end.timestamp()),
        ).fetchall():
            payload = _reflection_json(
                row["payload_json"],
                {},
                record_id=row["turn_id"],
                field="payload_json",
            )
            if not isinstance(payload, dict):
                continue
            stamp = self.context_timestamp(row["created_at"])
            if row["item_type"] in {"tool_call", "tool_result"}:
                call_id = str(payload.get("tool_call_id") or "unknown")
                identity = (str(row["turn_id"]), call_id)
                call = tool_calls.get(identity)
                if call is None:
                    call = {
                        "created_at": float(row["created_at"]),
                        "turn_id": str(row["turn_id"]),
                        "call_id": call_id,
                        "name": str(payload.get("name") or "unknown"),
                    }
                    tool_calls[identity] = call
                    tool_order.append(identity)
                if payload.get("name"):
                    call["name"] = str(payload["name"])
                if row["item_type"] == "tool_call":
                    call["source"] = str(payload.get("source") or "unknown")
                    call["arguments"] = payload.get("arguments", {})
                else:
                    call["result_trust"] = str(row["trust"])
                    call["ok"] = bool(payload.get("ok"))
                    if payload.get("error") not in (None, ""):
                        call["error"] = payload.get("error")
                    call["result"] = payload.get("result", {})
            elif row["item_type"] == "final":
                mood = payload.get("mood_change")
                if isinstance(mood, dict) and mood.get("state"):
                    mood_entries.append(
                        f"{stamp} state={mood.get('state')} "
                        f"intensity={mood.get('intensity', 'unknown')} "
                        f"cause={_reflection_compact_value(mood.get('cause'), 180)}"
                    )
                mutations = payload.get("mutations")
                if isinstance(mutations, dict):
                    for key, value in mutations.items():
                        if value in (None, [], {}, ""):
                            continue
                        if isinstance(value, list):
                            details = "; ".join(
                                _reflection_compact_value(item, 180) for item in value[:4]
                            )
                        else:
                            details = _reflection_compact_value(value, 300)
                        mutation_entries.append(f"{stamp} {key}: {details}")

        tool_entries: list[tuple[float, str, str, bool, bool]] = []
        for identity in tool_order:
            call = tool_calls[identity]
            details = [
                f"turn={call['turn_id']}",
                f"call={call['call_id']}",
                f"name={call['name']}",
                f"source={call.get('source', 'unknown')}",
                "arguments="
                + _reflection_compact_value(call.get("arguments", {}), 1200),
            ]
            if "ok" in call:
                details.append(f"ok={str(bool(call['ok'])).lower()}")
                if call.get("error") not in (None, ""):
                    details.append(
                        "error=" + _reflection_compact_value(call["error"], 400)
                    )
                details.append(
                    "result="
                    + _reflection_compact_value(call.get("result", {}), 1800)
                )
                details.append(
                    f"result_trust={call.get('result_trust', 'untrusted_tool_data')}"
                )
            else:
                details.append("result=(missing)")
            tool_entries.append(
                (
                    float(call["created_at"]),
                    f"TOOL TRACE {call['name']}",
                    " ".join(details),
                    False,
                    False,
                )
            )
        selected_tools = _reflection_select_entries(
            tool_entries,
            max(1000, min(8000, token_budget // 3)),
        )
        tool_timeline = "\n\n".join(
            f"[{self.context_timestamp(occurred_at)} {label}]\n{content}"
            for occurred_at, label, content, _, _ in selected_tools
        )

        topic_entries: list[str] = []
        episode_rows = self._db.execute(
            """SELECT id, title, status, working_summary, narrative_summary,
                      emotional_context_json, outcomes_json, topics_json,
                      open_loops_json, created_at, updated_at
               FROM conversation_episodes
               WHERE (created_at>=? AND created_at<?)
                  OR (updated_at>=? AND updated_at<?)
               ORDER BY updated_at""",
            (
                start.timestamp(),
                end.timestamp(),
                start.timestamp(),
                end.timestamp(),
            ),
        ).fetchall()
        for row in episode_rows:
            summary = str(row["narrative_summary"] or row["working_summary"] or "").strip()
            topics = _reflection_json(
                row["topics_json"], [], record_id=row["id"], field="topics_json"
            )
            loops = _reflection_json(
                row["open_loops_json"],
                [],
                record_id=row["id"],
                field="open_loops_json",
            )
            emotional = _reflection_json(
                row["emotional_context_json"],
                {},
                record_id=row["id"],
                field="emotional_context_json",
            )
            outcomes = _reflection_json(
                row["outcomes_json"],
                [],
                record_id=row["id"],
                field="outcomes_json",
            )
            parts = [
                f"{self.context_timestamp(row['updated_at'])} {row['status']} {row['title']}",
            ]
            if summary:
                parts.append(f"summary={_reflection_compact_value(summary, 320)}")
            if topics:
                parts.append(f"topics={_reflection_compact_value(topics, 180)}")
            if emotional:
                parts.append(f"emotional_context={_reflection_compact_value(emotional, 180)}")
            if outcomes:
                parts.append(f"outcomes={_reflection_compact_value(outcomes, 180)}")
            if loops:
                parts.append(f"open_loops={_reflection_compact_value(loops, 180)}")
            topic_entries.append("; ".join(parts))
            if len(topic_entries) >= 16:
                break
        return {
            "text": text,
            "owner_text": owner_text,
            "knowledge_text": knowledge_text,
            "entries": len(selected),
            "mood_timeline": truncate_tokens(
                "\n".join(mood_entries), 1200
            ) or "(no recorded mood changes)",
            "topic_timeline": truncate_tokens(
                "\n".join(topic_entries), 2600
            ) or "(no topic episode changed)",
            "mutation_timeline": truncate_tokens(
                "\n".join(mutation_entries), 2600
            ) or "(no recorded state mutations)",
            "tool_timeline": tool_timeline or "(no journaled tool calls)",
            "start_at": start.timestamp(),
            "end_at": end.timestamp(),
        }

    def commit_reflection(
        self,
        local_date: str,
        turn_id: str,
        summary: str,
        memories: list[dict[str, object]],
        conversation_actions: list[dict[str, object]] | None = None,
        maintenance_turn_id: str = "",
    ) -> None:
        reflection_id = f"reflection:{local_date}"
        now = time.time()
        with self._db:
            self._db.execute(
                """UPDATE reflections SET state='completed', claimed_at=NULL,
                   retry_at=NULL, summary=?, memories_json=?, error=NULL,
                   completed_at=? WHERE id=? AND state='running'""",
                (
                    summary,
                    json.dumps(memories, ensure_ascii=False, separators=(",", ":")),
                    now,
                    reflection_id,
                ),
            )
            self._db.execute(
                "DELETE FROM reflection_memories WHERE source_reflection_id=?",
                (reflection_id,),
            )
            for memory in memories:
                self._db.execute(
                    """INSERT INTO reflection_memories
                       (kind, key, content, evidence, confidence,
                        source_reflection_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(kind, key) DO UPDATE SET
                         content=excluded.content,
                         evidence=excluded.evidence,
                         confidence=excluded.confidence,
                         source_reflection_id=excluded.source_reflection_id,
                         updated_at=excluded.updated_at""",
                    (
                        memory["kind"],
                        memory["key"],
                        memory["content"],
                        memory["evidence"],
                        memory["confidence"],
                        reflection_id,
                        now,
                        now,
                    ),
                )
            self.apply_conversation_actions(conversation_actions or [], now=now)
            if maintenance_turn_id:
                self._db.execute(
                    """INSERT OR IGNORE INTO turns
                       (id, kind, workflow_kind, source_ids_json, state, stage,
                        started_at, updated_at)
                       VALUES (?, 'autonomous', 'memory_maintenance', ?, 'running',
                               'memory_maintenance_queued', ?, ?)""",
                    (
                        maintenance_turn_id,
                        json.dumps([reflection_id]),
                        now,
                        now,
                    ),
                )
            self._db.execute(
                """UPDATE turns SET state='completed', stage='completed',
                   failure_reason=NULL, updated_at=? WHERE id=?""",
                (now, turn_id),
            )

    def reflection(self, local_date: str) -> dict[str, object] | None:
        row = self._db.execute(
            "SELECT * FROM reflections WHERE local_date=?", (local_date,)
        ).fetchone()
        return dict(row) if row else None

    def list_reflections(
        self, limit: int = 14, *, before: str | None = None
    ) -> dict[str, object]:
        if limit <= 0:
            return {"items": []}
        size = min(366, max(1, int(limit)))
        query = "SELECT * FROM reflections"
        params: list[object] = []
        cursor = str(before or "").strip()
        if cursor:
            query += " WHERE local_date < ?"
            params.append(cursor)
        query += " ORDER BY local_date DESC LIMIT ?"
        params.append(size + 1)
        rows = self._db.execute(query, params).fetchall()
        extra = len(rows) > size
        results: list[dict[str, object]] = []
        for row in rows[:size]:
            item = dict(row)
            item["memories"] = decode_stored_json(
                item.pop("memories_json", "[]"),
                entity="reflection",
                record_id=item["id"],
                field="memories_json",
                expected_type=list,
                fallback=[],
            )
            add_context_timestamps(
                item,
                ("scheduled_at", "retry_at", "created_at", "completed_at"),
                self._timezone,
            )
            results.append(item)
        payload: dict[str, object] = {"items": results}
        if extra and results:
            payload["next_cursor"] = results[-1]["local_date"]
        return payload
