import json
import logging

from ...observability.events import log_event
from ...memory.text import estimate_tokens, truncate_tokens

logger = logging.getLogger(__name__)

TRANSCRIPT_PROTOCOL_TOOLS = frozenset(
    {
        "send_bubbles",
        "reply",
        "end_turn",
        "heartbeat_end_turn",
        "tool_enable",
        "tool_search",
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


# Goal reviews already persist an immutable result snapshot as an internal
# message. Only that explicitly sourced record belongs in the shared timeline.
_GOAL_RECORD_SQL = """(
    m.role='assistant' AND m.delivery_state='internal'
    AND json_extract(m.source_event_ids_json, '$[0]')='goal-record:' || m.turn_id
)"""

_HEARTBEAT_RECORD_SQL = """(
    m.role='assistant' AND m.delivery_state='internal'
    AND json_extract(m.source_event_ids_json, '$[0]')='heartbeat-record:' || m.turn_id
)"""

_PLAN_RECORD_SQL = """(
    m.role='assistant' AND m.delivery_state='internal'
    AND json_extract(m.source_event_ids_json, '$[0]')='plan-step-record:' || m.turn_id
)"""

_MESSAGE_TIME_SQL = """CASE WHEN m.role='event' THEN COALESCE(wr.created_at, m.created_at)
                            ELSE m.created_at END"""


class TranscriptStore:
    def turn_exchanges(self, turn_ids: list[str], *, window=None, include_reply_messages=False) -> dict[str, list[dict[str, object]]]:
        """Return completed native assistant/tool exchanges for transcript replay."""
        ordered_ids = [str(turn_id) for turn_id in dict.fromkeys(turn_ids) if turn_id]
        if not ordered_ids:
            return {}
        # Reply follow-up bubbles are archived under the original Owner Turn,
        # while their native exchanges belong to the follow-up executor Turn.
        # Replay both sessions for that shared timeline identity.
        parent_ids = {turn_id: turn_id for turn_id in ordered_ids}
        placeholders = ",".join("?" for _ in ordered_ids)
        linked = self._db.execute(
            f"""SELECT DISTINCT m.turn_id AS parent_id, o.turn_id AS executor_id
                FROM messages AS m JOIN outbox AS o ON o.id=m.outbox_id
                JOIN turns AS t ON t.id=o.turn_id
                WHERE m.turn_id IN ({placeholders})
                  AND o.turn_id != m.turn_id AND t.workflow_kind='reply_followup'
                  AND t.state='completed'""",
            tuple(ordered_ids),
        ).fetchall()
        for row in linked:
            parent_ids[str(row["executor_id"])] = str(row["parent_id"])
        if window is not None:
            # A partially observed turn cannot be replayed wholesale: tool calls
            # or speech on either side of the review boundary would leak in.
            excluded = set()
            for identifier, parent in parent_ids.items():
                bounds = self._db.execute(
                    "SELECT started_at, updated_at, state FROM turns WHERE id=?", (identifier,)
                ).fetchone()
                if (bounds is None or bounds["state"] != "completed"
                        or bounds["started_at"] < window[0] or bounds["updated_at"] >= window[1]):
                    excluded.add(parent)
                if self._db.execute(
                    "SELECT 1 FROM turn_journal WHERE turn_id=? AND item_type='assistant_exchange' "
                    "AND (created_at<? OR created_at>=?) LIMIT 1", (identifier, *window)
                ).fetchone():
                    excluded.add(parent)
            parent_ids = {key: value for key, value in parent_ids.items() if value not in excluded}
            if not parent_ids:
                return {}
        ordered_ids = list(parent_ids)
        placeholders = ",".join("?" for _ in ordered_ids)
        rows = self._db.execute(
            f"""SELECT j.turn_id, j.payload_json FROM turn_journal AS j
                JOIN turns AS t ON t.id=j.turn_id
                WHERE j.turn_id IN ({placeholders}) AND j.item_type='assistant_exchange'
                ORDER BY t.started_at, j.sequence""",
            tuple(ordered_ids),
        ).fetchall()
        exchanges: dict[str, list[dict[str, object]]] = {}
        executor_exchanges = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except ValueError:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("content"), (str, list)):
                exchanges.setdefault(parent_ids[str(row["turn_id"])], []).append(payload)
                executor_exchanges.append((str(row["turn_id"]), payload))
        if include_reply_messages and any(
            block.get("type") == "tool_use" and block.get("name") == "reply"
            for items in exchanges.values() for item in items
            for block in item["content"] if isinstance(item["content"], list) and isinstance(block, dict)
        ):
            speech = {}
            records = self._db.execute(
                f"""SELECT p.turn_id, p.tool_call_id, m.content, m.created_at,
                           m.delivery_state, o.kind
                    FROM turn_progress p
                    JOIN outbox o ON o.dedupe_key = 'turn:' || p.turn_id || ':progress:' ||
                         p.tool_call_id || ':' || p.part_index
                    JOIN messages m ON m.outbox_id=o.id AND m.role='assistant'
                    WHERE p.turn_id IN ({placeholders})
                    ORDER BY p.turn_id, p.tool_call_id, p.part_index""",
                tuple(ordered_ids),
            ).fetchall()
            for row in records:
                speech.setdefault((str(row["turn_id"]), str(row["tool_call_id"])), []).append({
                    key: row[key] for key in ("content", "created_at", "delivery_state", "kind")
                })
            for executor, item in executor_exchanges:
                # Call IDs are local to an executor, including follow-ups
                # archived under the original owner Turn.
                calls = item["content"] if isinstance(item["content"], list) else []
                item["_reply_messages"] = {
                    block["id"]: speech[(executor, block["id"])]
                    for block in calls if isinstance(block, dict) and block.get("name") == "reply"
                    and (executor, block.get("id")) in speech
                }
        return exchanges

    def transcript_window_turn_limit(
        self, minimum_turns: int, maximum_turns: int, *, force_compact: bool = False
    ) -> int:
        minimum_turns = max(1, minimum_turns)
        maximum_turns = max(minimum_turns, maximum_turns)
        latest = self._db.execute(
            f"""SELECT t.id, t.updated_at FROM turns AS t
               WHERE t.state='completed' AND EXISTS (
                   SELECT 1 FROM messages AS m
                   WHERE m.turn_id=t.id
                     AND (
                         m.role IN ('user', 'event') OR {_GOAL_RECORD_SQL} OR {_HEARTBEAT_RECORD_SQL} OR {_PLAN_RECORD_SQL}
                         OR m.role='assistant'
                            AND m.delivery_state IN ('delivered', 'uncertain', 'queued')
                     )
               )
               ORDER BY t.updated_at DESC, t.id DESC LIMIT 1"""
        ).fetchone()
        if latest is None:
            if force_compact:
                with self._db:
                    self._db.execute("DELETE FROM transcript_enabled_tools")
            return minimum_turns
        # An existing Turn may be updated after completion (reply follow-ups
        # do this). Its updated_at moving forward must not grow the window.
        visible_total = int(self._db.execute(
            f"""SELECT COUNT(*) FROM turns AS t
                WHERE t.state='completed' AND EXISTS (
                    SELECT 1 FROM messages AS m
                    WHERE m.turn_id=t.id
                      AND (
                          m.role IN ('user', 'event') OR {_GOAL_RECORD_SQL}
                          OR {_HEARTBEAT_RECORD_SQL} OR {_PLAN_RECORD_SQL}
                          OR m.role='assistant'
                             AND m.delivery_state IN ('delivered', 'uncertain', 'queued')
                      )
                )"""
        ).fetchone()[0])
        with self._db:
            state = self._db.execute(
                "SELECT * FROM transcript_window_state WHERE id=1"
            ).fetchone()
            if state is None:
                self._db.execute(
                    """INSERT INTO transcript_window_state
                       (id, current_turns, observed_turn_id, observed_updated_at,
                        observed_total_turns)
                       VALUES (1, ?, ?, ?, ?)""",
                    (minimum_turns, latest["id"], latest["updated_at"], visible_total),
                )
                return minimum_turns
            observed_total = state["observed_total_turns"]
            new_turns = (
                max(0, visible_total - int(observed_total))
                if observed_total is not None else 0
            )
            current = min(
                maximum_turns,
                max(minimum_turns, int(state["current_turns"])),
            )
            span = maximum_turns - minimum_turns
            compacted = force_compact or (
                span > 0 and current - minimum_turns + new_turns >= span
            )
            current = (
                minimum_turns
                if force_compact or span == 0
                else minimum_turns + (current - minimum_turns + new_turns) % span
            )
            self._db.execute(
                """UPDATE transcript_window_state
                   SET current_turns=?, observed_turn_id=?, observed_updated_at=?,
                       observed_total_turns=?
                   WHERE id=1""",
                (current, latest["id"], latest["updated_at"], visible_total),
            )
            if compacted:
                self._db.execute("DELETE FROM transcript_enabled_tools")
        if compacted:
            memory_state = self._db.execute(
                "SELECT data_json FROM transcript_memory_state WHERE id=1"
            ).fetchone()
            if memory_state:
                with self._db:
                    data = json.loads(memory_state[0])
                    data["pending_compact"] = True
                    self._db.execute(
                        "UPDATE transcript_memory_state SET data_json=? WHERE id=1",
                        (json.dumps(data, ensure_ascii=False),),
                    )
            log_event(
                logger,
                logging.INFO,
                "transcript_window_compacted",
                reason="manual" if force_compact else "window_limit",
                retained_turns=current,
                minimum_turns=minimum_turns,
                maximum_turns=maximum_turns,
                observed_new_turns=new_turns,
            )
        return current

    def replyer_dialogue_rows(self, channel: str, *, limit: int = 96):
        """Actual messages, including the active turn; never journal/tool text."""
        channels = ("napcat", "qq_call") if channel in {"napcat", "qq_call"} else (channel, channel)
        rows = self._db.execute("""SELECT m.id, m.role, m.content, m.created_at,
                    m.delivery_state, m.turn_id FROM messages m
                    LEFT JOIN outbox o ON o.id=m.outbox_id
                    WHERE ((m.role='user' AND EXISTS (
                        SELECT 1 FROM json_each(m.source_event_ids_json) src
                        JOIN events e ON e.id=src.value
                        WHERE json_extract(e.payload_json, '$.channel') IN (?, ?))) OR
                        (m.role='assistant' AND m.delivery_state='delivered'
                         AND o.target_channel IN (?, ?)))
                    ORDER BY m.id DESC LIMIT ?""", (*channels, *channels, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def recent_conversation_messages(
        self,
        turn_limit: int,
        token_budget: int,
        before_timestamp: float | None = None,
        *, include_images: bool = False,
    ) -> list[dict[str, object]]:
        if turn_limit <= 0 or token_budget <= 0:
            return []
        turns = self._db.execute(
            f"""SELECT t.id, t.updated_at FROM turns AS t
               WHERE t.state='completed' AND EXISTS (
                   SELECT 1 FROM messages AS m
                   WHERE m.turn_id=t.id
                     AND (
                         m.role IN ('user', 'event') OR {_GOAL_RECORD_SQL} OR {_HEARTBEAT_RECORD_SQL} OR {_PLAN_RECORD_SQL}
                         OR m.role='assistant'
                            AND m.delivery_state IN ('delivered', 'uncertain', 'queued')
                     )
               )
                 AND (? IS NULL OR t.updated_at < ?)
               ORDER BY t.updated_at DESC LIMIT ?""",
            (before_timestamp, before_timestamp, turn_limit),
        ).fetchall()
        if not turns:
            return []
        turn_ids = [str(row["id"]) for row in turns]
        rows = self.conversation_messages_for_turns(turn_ids)
        if include_images:
            rows = self.image_history_rows(rows)
        by_turn: dict[str, list[dict[str, object]]] = {}
        for item in rows:
            by_turn.setdefault(str(item["turn_id"]), []).append(item)
        selected: list[list[dict[str, object]]] = []
        used = 0
        for turn_id in turn_ids:
            group = by_turn.get(turn_id, [])
            if not group:
                continue
            size = sum(estimate_tokens(str(item["content"])) for item in group)
            if selected and used + size > token_budget:
                break
            selected.append(group)
            used += size
        return [item for group in reversed(selected) for item in group]

    def conversation_messages_for_turns(self, turn_ids, *, window=None):
        """Use the shared timeline projection without dropping required evidence."""
        if turn_ids is not None and not turn_ids:
            return []
        if turn_ids is None and window is None:
            raise ValueError("conversation window is required without turn ids")
        scope = "1"
        parameters = []
        if turn_ids is not None:
            scope = f"m.turn_id IN ({','.join('?' for _ in turn_ids)})"
            parameters.extend(turn_ids)
        if window is not None:
            scope += f" AND ({_MESSAGE_TIME_SQL})>=? AND ({_MESSAGE_TIME_SQL})<?"
            parameters.extend(window)
        rows = self._db.execute(
            f"""SELECT m.id, m.turn_id,
                       CASE WHEN {_GOAL_RECORD_SQL} THEN 'goal'
                            WHEN {_HEARTBEAT_RECORD_SQL} THEN 'heartbeat' WHEN {_PLAN_RECORD_SQL} THEN 'plan_step' ELSE m.role END AS role,
                       m.content,
                       {_MESSAGE_TIME_SQL} AS created_at,
                       m.delivery_state,
                       t.failure_reason AS turn_failure_reason,
                       CASE WHEN m.role='event'
                            THEN CASE WHEN t.workflow_kind='channel_event'
                                      THEN COALESCE((
                                          SELECT json_extract(e.payload_json, '$.channel') || ':' ||
                                                 json_extract(e.payload_json, '$.notice_type')
                                          FROM json_each(m.source_event_ids_json) src
                                          JOIN events e ON e.id=src.value LIMIT 1
                                      ), 'channel:notice')
                                      ELSE 'webhook:' || COALESCE(wr.workflow_id, 'unknown') END
                            ELSE '' END AS event_source
                FROM messages AS m
                LEFT JOIN turns AS t ON t.id=m.turn_id
                LEFT JOIN webhook_steps AS ws
                  ON m.turn_id=('webhook:' || ws.run_id || ':' || ws.step_index)
                LEFT JOIN webhook_runs AS wr ON wr.id=ws.run_id
                WHERE {scope}
                  AND ({_GOAL_RECORD_SQL} OR {_HEARTBEAT_RECORD_SQL} OR {_PLAN_RECORD_SQL} OR m.role IN ('user', 'event') OR m.delivery_state IN ('delivered', 'uncertain', 'queued'))
                ORDER BY m.id""",
            tuple(parameters),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["timestamp"] = self.context_timestamp(item["created_at"])
            result.append(item)
        return result

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
                    **({"recall_result": result} if payload.get("name") == "recall" else {}),
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
                    **({"recall_arguments": payload.get("arguments")} if name == "recall" else {}),
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
