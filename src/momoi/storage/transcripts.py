import json
import logging
import time

from ..observability.events import log_event
from .context_plan_adapter import normalize_context_plan
from .integrity import decode_stored_json
from .memory_values import estimate_tokens, truncate_tokens
from .turn_workflow import turn_workflow_kind_sql

logger = logging.getLogger(__name__)

TRANSCRIPT_PROTOCOL_TOOLS = frozenset(
    {
        "send_bubbles",
        "end_turn",
        "autonomous_finish",
        "heartbeat_end_turn",
        "tool_enable",
        "read_tool_result",
    }
)
_TOOL_SUBJECT_KEYS = (
    "query", "q", "expression", "url", "path", "title", "name",
    "key", "keyword", "command",
)


def _tool_call_subject(arguments: object, limit: int = 48) -> str:
    if not isinstance(arguments, dict):
        return ""
    for key in _TOOL_SUBJECT_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return truncate_tokens(" ".join(value.split()), limit)
    for value in arguments.values():
        if isinstance(value, str) and value.strip():
            return truncate_tokens(" ".join(value.split()), limit)
    return ""


class TranscriptStore:
    def transcript_window_turn_limit(
        self, minimum_turns: int, maximum_turns: int
    ) -> int:
        minimum_turns = max(1, minimum_turns)
        maximum_turns = max(minimum_turns, maximum_turns)
        latest = self._db.execute(
            """SELECT t.id, t.updated_at FROM turns AS t
               WHERE t.state='completed' AND EXISTS (
                   SELECT 1 FROM messages AS m
                   WHERE m.turn_id=t.id
                     AND (
                         m.role='user'
                         OR m.role='assistant'
                            AND m.delivery_state IN ('delivered', 'uncertain')
                     )
               )
               ORDER BY t.updated_at DESC, t.id DESC LIMIT 1"""
        ).fetchone()
        if latest is None:
            return minimum_turns
        with self._db:
            state = self._db.execute(
                "SELECT * FROM transcript_window_state WHERE id=1"
            ).fetchone()
            if state is None:
                self._db.execute(
                    """INSERT INTO transcript_window_state
                       (id, current_turns, observed_turn_id, observed_updated_at)
                       VALUES (1, ?, ?, ?)""",
                    (minimum_turns, latest["id"], latest["updated_at"]),
                )
                return minimum_turns
            new_turns = int(
                self._db.execute(
                    """SELECT COUNT(*) FROM turns AS t
                       WHERE t.state='completed'
                         AND (
                             t.updated_at>?
                             OR t.updated_at=? AND t.id>?
                         )
                         AND EXISTS (
                             SELECT 1 FROM messages AS m
                             WHERE m.turn_id=t.id
                               AND (
                                   m.role='user'
                                   OR m.role='assistant'
                                      AND m.delivery_state IN (
                                          'delivered', 'uncertain'
                                      )
                               )
                         )""",
                    (
                        state["observed_updated_at"],
                        state["observed_updated_at"],
                        state["observed_turn_id"],
                    ),
                ).fetchone()[0]
            )
            current = min(
                maximum_turns,
                max(minimum_turns, int(state["current_turns"])),
            )
            span = maximum_turns - minimum_turns
            compacted = span > 0 and current - minimum_turns + new_turns >= span
            current = (
                minimum_turns
                if span == 0
                else minimum_turns + (current - minimum_turns + new_turns) % span
            )
            self._db.execute(
                """UPDATE transcript_window_state
                   SET current_turns=?, observed_turn_id=?, observed_updated_at=?
                   WHERE id=1""",
                (current, latest["id"], latest["updated_at"]),
            )
        if compacted:
            log_event(
                logger,
                logging.INFO,
                "transcript_window_compacted",
                retained_turns=current,
                minimum_turns=minimum_turns,
                maximum_turns=maximum_turns,
                observed_new_turns=new_turns,
            )
        return current

    def recent_conversation_messages(
        self,
        turn_limit: int,
        token_budget: int,
        before_timestamp: float | None = None,
    ) -> list[dict[str, object]]:
        if turn_limit <= 0 or token_budget <= 0:
            return []
        turns = self._db.execute(
            """SELECT t.id, t.updated_at FROM turns AS t
               WHERE t.state='completed' AND EXISTS (
                   SELECT 1 FROM messages AS m
                   WHERE m.turn_id=t.id
                     AND (
                         m.role='user'
                         OR m.role='assistant'
                            AND m.delivery_state IN ('delivered', 'uncertain')
                     )
               )
                 AND (? IS NULL OR t.updated_at < ?)
               ORDER BY t.updated_at DESC LIMIT ?""",
            (before_timestamp, before_timestamp, turn_limit),
        ).fetchall()
        if not turns:
            return []
        turn_ids = [str(row["id"]) for row in turns]
        placeholders = ",".join("?" for _ in turn_ids)
        rows = self._db.execute(
            f"""SELECT m.id, m.turn_id, m.role, m.content, m.created_at,
                       m.delivery_state
                FROM messages AS m
                WHERE m.turn_id IN ({placeholders})
                  AND (m.role IN ('user', 'event') OR m.delivery_state IN ('delivered', 'uncertain'))
                ORDER BY m.id""",
            tuple(turn_ids),
        ).fetchall()
        by_turn: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            item = dict(row)
            item["timestamp"] = self.context_timestamp(item["created_at"])
            by_turn.setdefault(str(row["turn_id"]), []).append(item)
        selected: list[list[dict[str, object]]] = []
        used = 0
        for turn_id in turn_ids:
            group = by_turn.get(turn_id, [])
            if not group:
                continue
            size = sum(estimate_tokens(str(item["content"])) for item in group)
            if selected and used + size > token_budget:
                break
            if not selected and size > token_budget:
                per_message = max(1, token_budget // len(group))
                for item in group:
                    item["content"] = truncate_tokens(str(item["content"]), per_message)
                size = sum(estimate_tokens(str(item["content"])) for item in group)
            selected.append(group)
            used += size
        return [item for group in reversed(selected) for item in group]

    def turn_activity(self, turn_ids: list[str]) -> dict[str, list[dict[str, object]]]:
        """Return each Turn's work in the order it happened.

        Historical tool results are far too large to replay, but dropping them
        entirely leaves Momoi claiming actions with nothing behind them: a reply
        saying it checked a subscription reads identically whether the check
        succeeded, failed or never ran. Keeping the call, its subject, its
        outcome and any stored result reference preserves that accountability
        and still lets the exact payload be reread on demand.

        Records carry their timestamp so the caller can interleave them with the
        bubbles the same Turn delivered, which is what makes a reply readable as
        "said this, then did that, then reported the result".
        """

        ordered_ids = [str(turn_id) for turn_id in dict.fromkeys(turn_ids) if turn_id]
        if not ordered_ids:
            return {}
        placeholders = ",".join("?" for _ in ordered_ids)
        rows = self._db.execute(
            f"""SELECT turn_id, sequence, created_at, item_type, payload_json
                FROM turn_journal
                WHERE turn_id IN ({placeholders})
                  AND item_type IN ('tool_call', 'tool_result')
                ORDER BY turn_id, sequence""",
            tuple(ordered_ids),
        ).fetchall()
        outcomes: dict[str, dict[str, object]] = {}
        calls: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            call_id = str(payload.get("tool_call_id") or "")
            if str(row["item_type"]) == "tool_result":
                result = payload.get("result")
                outcomes[call_id] = {
                    "ok": bool(payload.get("ok")),
                    "error": " ".join(str(payload.get("error") or "").split())[:80],
                    "ref": str(result.get("result_ref") or "")
                    if isinstance(result, dict)
                    else "",
                }
                continue
            name = str(payload.get("name") or "")
            if not name or name in TRANSCRIPT_PROTOCOL_TOOLS:
                continue
            calls.setdefault(str(row["turn_id"]), []).append(
                {
                    "at": float(row["created_at"]),
                    "call_id": call_id,
                    "name": name,
                    "subject": _tool_call_subject(payload.get("arguments")),
                }
            )
        for records in calls.values():
            for record in records:
                record.update(
                    outcomes.get(
                        str(record.pop("call_id")), {"ok": True, "error": "", "ref": ""}
                    )
                )
        return calls

    def recent_turn_records(
        self,
        turn_limit: int,
        before_timestamp: float | None = None,
    ) -> list[dict[str, object]]:
        if turn_limit <= 0:
            return []
        workflow = turn_workflow_kind_sql("t")
        turns = self._db.execute(
            f"""SELECT t.*, {workflow} AS resolved_workflow_kind
               FROM turns AS t
               WHERE t.state<>'running'
                 AND (? IS NULL OR t.updated_at < ?)
                 AND (
                     t.kind='owner' OR EXISTS (
                         SELECT 1 FROM messages AS m
                         WHERE m.turn_id=t.id
                           AND m.role='assistant'
                           AND m.delivery_state IN ('delivered', 'uncertain')
                     )
                 )
               ORDER BY t.updated_at DESC LIMIT ?""",
            (before_timestamp, before_timestamp, turn_limit),
        ).fetchall()
        records: list[dict[str, object]] = []
        for turn in reversed(turns):
            turn_id = str(turn["id"])
            timeline: list[dict[str, object]] = []
            for message in self._db.execute(
                """SELECT id, role, content, created_at, delivery_state
                   FROM messages WHERE turn_id=? ORDER BY id""",
                (turn_id,),
            ).fetchall():
                role = str(message["role"])
                timeline.append(
                    {
                        "type": (
                            "owner_message"
                            if role == "user"
                            else ("event" if role == "event" else "assistant_message")
                        ),
                        "timestamp": self.context_timestamp(message["created_at"]),
                        "text": str(message["content"]),
                        "delivery": str(message["delivery_state"]),
                        "trust": "owner" if role == "user" else "context_data",
                        "_sort": (
                            float(message["created_at"]),
                            0,
                            int(message["id"]),
                        ),
                    }
                )
            final: dict[str, object] = {
                "state": str(turn["state"]),
                "external_effect": bool(turn["external_effect_started"]),
                "failure": str(turn["failure_reason"] or ""),
                "llm": {
                    "calls": int(turn["llm_calls"]),
                    "input_tokens": int(turn["input_tokens"]),
                    "output_tokens": int(turn["output_tokens"]),
                },
            }
            for item in self._db.execute(
                """SELECT sequence, created_at, item_type, visibility, trust,
                          payload_json
                   FROM turn_journal WHERE turn_id=?
                   ORDER BY sequence""",
                (turn_id,),
            ).fetchall():
                payload = decode_stored_json(
                    item["payload_json"],
                    entity="turn_journal",
                    record_id=f"{turn_id}:{item['sequence']}",
                    field="payload_json",
                    expected_type=dict,
                    fallback={"error": "invalid_journal_payload"},
                )
                if item["item_type"] == "final":
                    final.update(payload)
                    continue
                timeline.append(
                    {
                        "type": str(item["item_type"]),
                        "timestamp": self.context_timestamp(item["created_at"]),
                        "visibility": str(item["visibility"]),
                        "trust": str(item["trust"]),
                        **payload,
                        "_sort": (
                            float(item["created_at"]),
                            1,
                            int(item["sequence"]),
                        ),
                    }
                )
            timeline.sort(key=lambda item: item["_sort"])
            for item in timeline:
                item.pop("_sort", None)
            plan_row = self._db.execute(
                """SELECT plan_json FROM context_plans WHERE turn_id=?
                   ORDER BY revision DESC LIMIT 1""",
                (turn_id,),
            ).fetchone()
            interpretation: dict[str, object] = {}
            if plan_row is not None:
                raw_plan = decode_stored_json(
                    plan_row["plan_json"],
                    entity="context_plan",
                    record_id=turn_id,
                    field="plan_json",
                    expected_type=dict,
                    fallback={},
                )
                plan = normalize_context_plan(raw_plan)
                if isinstance(plan, dict):
                    units = plan.get("intent_units")
                    interpretation = {
                        "intents": [
                            {
                                key: unit.get(key)
                                for key in (
                                    "id",
                                    "text",
                                    "intent",
                                    "speech_act",
                                    "references",
                                )
                                if key in unit
                            }
                            for unit in units or []
                            if isinstance(unit, dict)
                        ],
                        "episode_actions": [
                            {
                                key: action.get(key)
                                for key in (
                                    "action",
                                    "episode_ref",
                                    "episode_id",
                                    "title",
                                    "unit_ids",
                                )
                                if key in action
                            }
                            for action in plan.get("episode_actions", [])
                            if isinstance(action, dict)
                        ],
                        "uncertainty": [
                            str(value) for value in plan.get("uncertainty", [])
                        ],
                    }
            records.append(
                {
                    "turn_id": turn_id,
                    "kind": str(turn["kind"]),
                    "workflow_kind": str(turn["resolved_workflow_kind"] or ""),
                    "state": str(turn["state"]),
                    "channel": str(final.get("channel") or ""),
                    "started_at": self.context_timestamp(turn["started_at"]),
                    "completed_at": self.context_timestamp(turn["updated_at"]),
                    "interpretation": interpretation,
                    "timeline": timeline,
                    "final": final,
                }
            )
        return records

    def recent_external_events(
        self,
        limit: int,
        lookback_seconds: float,
        before_timestamp: float | None = None,
    ) -> list[dict[str, object]]:
        """Return folded autonomous Events that never became shared dialogue."""

        if limit <= 0 or lookback_seconds <= 0:
            return []
        upper = float(before_timestamp) if before_timestamp is not None else time.time()
        rows = self._db.execute(
            """SELECT m.content, m.created_at, t.id AS turn_id,
                      t.source_ids_json, wr.workflow_id
               FROM messages AS m
               JOIN turns AS t ON t.id=m.turn_id
               LEFT JOIN webhook_steps AS ws
                 ON t.id=('webhook:' || ws.run_id || ':' || ws.step_index)
               LEFT JOIN webhook_runs AS wr ON wr.id=ws.run_id
               WHERE t.kind='autonomous'
                 AND t.state<>'running'
                 AND t.updated_at>=? AND t.updated_at<?
                 AND m.role='event'
                 AND m.created_at>=? AND m.created_at<?
                 AND NOT EXISTS (
                     SELECT 1 FROM messages AS visible
                     WHERE visible.turn_id=t.id
                       AND visible.role='assistant'
                       AND visible.delivery_state IN ('delivered', 'uncertain')
                 )
               ORDER BY m.created_at""",
            (
                upper - float(lookback_seconds),
                upper,
                upper - float(lookback_seconds),
                upper,
            ),
        ).fetchall()
        folded: dict[tuple[str, str], dict[str, object]] = {}
        for row in rows:
            content = " ".join(str(row["content"] or "").split())
            if not content:
                continue
            workflow_id = str(row["workflow_id"] or "").strip()
            if workflow_id:
                source = f"webhook:{workflow_id}"
            else:
                source_ids = decode_stored_json(
                    row["source_ids_json"] or "[]",
                    entity="turn",
                    record_id=row["turn_id"],
                    field="source_ids_json",
                    expected_type=list,
                    fallback=[],
                )
                raw_source = str(source_ids[0]) if source_ids else str(row["turn_id"])
                source = raw_source.split(":", 1)[0] or "autonomous"
            key = (source, content)
            seen_at = float(row["created_at"])
            item = folded.get(key)
            if item is None:
                folded[key] = {
                    "source": source,
                    "event": content,
                    "first_seen": seen_at,
                    "last_seen": seen_at,
                    "occurrences": 1,
                }
                continue
            item["last_seen"] = seen_at
            item["occurrences"] = int(item["occurrences"]) + 1
        selected = sorted(
            folded.values(),
            key=lambda item: (float(item["last_seen"]), str(item["source"])),
        )[-limit:]
        return selected
