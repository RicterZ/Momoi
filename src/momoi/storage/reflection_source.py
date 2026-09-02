from datetime import datetime, timedelta

from .memory_values import truncate_tokens
from .reflection_values import (
    _reflection_compact_value,
    _reflection_json,
    _reflection_select_entries,
)


class ReflectionSourceStore:
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

