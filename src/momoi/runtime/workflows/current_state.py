"""A private standard workflow following committed conversational end_turns."""

import asyncio
import copy
import json
import logging
from time import time
from xml.sax.saxutils import escape, quoteattr

from ...models import IncomingMessage, TurnDraft
from ...observability.events import log_event
from ...storage.memory.current_state import StateConflict
from ..agent import AgentWorkflow
from ..context.current_state import pack_current_turn_context
from ..transcript.rendering import render_pending_turns
from ..turn_support import PROMPT_ROOT, live_prompt

logger = logging.getLogger(__name__)
PROMPT_PATH = PROMPT_ROOT.joinpath("current_state.md")


class CurrentStateWorkflow:
    def _source_turn_owner_events(
        self, source_turn_ids: list[str]
    ) -> list[IncomingMessage]:
        """Owner events behind the maintained Turns, for memory evidence checks."""

        placeholders = ",".join("?" for _ in source_turn_ids)
        rows = self.store._db.execute(
            f"""SELECT source_event_ids_json FROM messages
                WHERE turn_id IN ({placeholders}) AND role='user'""",
            tuple(source_turn_ids),
        ).fetchall()
        event_ids: list[str] = []
        for (source_json,) in rows:
            try:
                ids = json.loads(str(source_json))
            except (TypeError, ValueError):
                continue
            if isinstance(ids, list):
                event_ids.extend(str(item) for item in ids)
        event_ids = list(dict.fromkeys(event_ids))
        if not event_ids:
            return []
        placeholders = ",".join("?" for _ in event_ids)
        event_rows = self.store._db.execute(
            f"SELECT * FROM events WHERE id IN ({placeholders})"
            " ORDER BY received_at, rowid",
            tuple(event_ids),
        ).fetchall()
        return [self.store._incoming_message(row) for row in event_rows]

    async def _complete_current_state_task(self, source_turn_id: str) -> None:
        # Reserve most of the window for the reusable prefix/history. This is
        # a conservative character heuristic, not a provider token measurement.
        batch = self.store.claim_current_state_batch(
            source_turn_id,
            max_source_chars=max(1024, self._context_compaction_tokens() // 4),
        )
        if batch is None:
            return
        turn_id = batch["turn_id"]
        source_turn_ids = batch["source_turn_ids"]
        try:
            await asyncio.wait_for(
                self._run_current_state_task(batch),
                timeout=self.config.current_state.max_seconds,
            )
        except asyncio.CancelledError:
            self.store.release_current_state_batch(
                source_turn_ids, turn_id, "cancelled", interrupted=True
            )
            raise
        except Exception as error:
            self.store.release_current_state_batch(
                source_turn_ids, turn_id, type(error).__name__
            )
            log_event(
                logger,
                logging.WARNING,
                "current_state_maintenance_failed",
                stage="current_state_maintenance",
                turn_id=turn_id,
                source_turn_ids=source_turn_ids,
                error_type=type(error).__name__,
            )
        else:
            log_event(
                logger,
                logging.INFO,
                "current_state_maintenance_complete",
                stage="current_state_maintenance",
                turn_id=turn_id,
                source_turn_ids=source_turn_ids,
            )
        finally:
            self.agenda_changed.set()

    async def _run_current_state_task(self, batch):
        tasks = batch["tasks"]
        source_turn_ids = batch["source_turn_ids"]
        source_turn_id = batch["source_turn_id"]
        turn_id = batch["turn_id"]
        complete = False
        snapshot = self.store.current_state.snapshot()
        latest = pack_current_turn_context(
            self.store,
            tasks[-1]["source_stage"],
            (
                "self_state",
                f"Current local time: {self.store.context_timestamp(time())}",
            ),
            ("state_update_contract", live_prompt(PROMPT_PATH, "")),
            include_empty=True,
            maintenance=True,
        )
        shared = self.shared_turn_context(turn_id)
        # Resolve provenance against original records, never request-only annotations
        # such as generated image summaries.
        rows = {
            row["id"]: row
            for row in self.store.conversation_messages_for_turns(source_turn_ids)
        }
        turn_labels = {value: f"T-{index}" for index, value in enumerate(source_turn_ids, 1)}
        messages = copy.deepcopy(shared["messages"])
        evidence_rows = state_evidence_rows(self.store, rows.values())
        injected = shared["memories"]
        events = self._source_turn_owner_events(source_turn_ids)
        # Snapshot the transcript before the tool loop mutates the live list;
        # a queued memory review must never capture this Turn's own tool rounds.
        draft = TurnDraft(memory_context=injected, memory_conversation=[*messages])
        labels = [turn_labels[value] for value in source_turn_ids]
        evidence = render_state_evidence(self.store, evidence_rows, turn_labels)
        request = (
            render_pending_turns(labels)
            + "\n\n<source_evidence>\n" + evidence + "\n</source_evidence>\n\n"
            + latest
        )
        messages.append({"role": "user", "content": request})
        # Use the same live public prefix as Owner/Heartbeat/Plan. Queued
        # snapshots can predate SOUL edits and dynamic tool enable changes.
        system = self._system()
        tools = self.tool_surface.conversation_specs()

        async def execute_tool(call):
            nonlocal complete
            if not self.store.current_state_batch_is_current(source_turn_ids, turn_id):
                raise StateConflict("source_turn_no_longer_current")
            if call.name == "memory_operation":
                if call.arguments.get("scope") == "current_state":
                    return {"ok": False, "error": "use_current_state_finish",
                            "message": "In this maintenance workflow, submit state changes through current_state_finish."}
                return self.memory_tools.execute(call, events, draft)
            try:
                self.store.current_state.apply_arguments(
                    resolve_state_evidence(call.arguments, evidence_rows, turn_labels),
                    source_turn_id=source_turn_id,
                    operation_id=f"current-state:{turn_id}",
                    expected_revision=snapshot.revision,
                )
            except StateConflict:
                # A fresh retry must read a fresh snapshot, not blindly overwrite it.
                raise
            except ValueError as error:
                return {"ok": False, "error": str(error)}
            self.store._queue_memory_operations(turn_id, draft, events, time())
            self.store.finish_current_state_batch(source_turn_ids, turn_id)
            complete = True
            return {"ok": True, "state": "completed"}

        workflow = AgentWorkflow(
            stage="current_state_maintenance",
            tool_names=frozenset({"current_state_finish", "memory_operation"}),
            execute_tool=execute_tool,
            is_complete=lambda: complete,
            completion_result=lambda: {"ok": True} if complete else None,
            no_tool_correction="Submit current_state_finish using its schema. Do not reply to the owner.",
        )
        await self._run_agent_workflow(
            system, messages, tools, turn_id, workflow,
            current_events=events,
        )
        if not complete:
            raise RuntimeError("state_maintenance_incomplete")


def resolve_state_evidence(arguments, rows, labels):
    """Resolve exact quotes against supplied records, never model-provided clocks."""
    resolved = copy.deepcopy(arguments)
    by_label = {label: turn_id for turn_id, label in labels.items()}
    for item in resolved.get("add", []):
        label = item.pop("source_turn", "")
        quote = item.pop("source", "")
        source_id = item.pop("source_id", "")
        turn_id = by_label.get(label)
        matches = [
            row
            for row in rows
            if source_id and row.get("source_id") == source_id
            and row.get("turn_id") == turn_id
            and row.get("role") in {"user", "assistant", "event"}
            and isinstance(quote, str)
            and quote.strip()
            and quote in str(row.get("content") or "")
        ]
        if len(matches) != 1:
            raise ValueError("source_quote_must_match_one_message_in_source_turn")
        row = matches[0]
        # A spoken assistant claim is not evidence about somebody else's state.
        if row["role"] == "assistant" and item.get("status") == "observed":
            raise ValueError("assistant_source_requires_inferred_status")
        item.update(
            evidence_turn_id=turn_id,
            source_quote=quote,
            source_role=row["role"],
            observed_at=float(row["created_at"]),
        )
    return resolved


def state_evidence_rows(store, rows):
    """A batched user bubble's timestamp is not each source event's timestamp."""
    result = []
    for original in rows:
        row = {**original, "source_id": f"message:{original['id']}"}
        if row.get("role") != "user":
            result.append(row)
            continue
        record = store._db.execute(
            "SELECT source_event_ids_json FROM messages WHERE id=?", (row["id"],)
        ).fetchone()
        events = []
        for event_id in json.loads(record[0] or "[]") if record else []:
            event = store._db.execute(
                "SELECT content,received_at FROM events WHERE id=?", (event_id,)
            ).fetchone()
            if event is not None:
                events.append(
                    {
                        **row,
                        "source_id": f"event:{event_id}",
                        "content": event["content"],
                        "created_at": event["received_at"],
                    }
                )
        result.extend(events or [row])
    return result


def render_state_evidence(store, rows, labels):
    """Exact original evidence, distinct from the shared native transcript."""
    parts = []
    for turn_id, label in labels.items():
        sources = [row for row in rows if row["turn_id"] == turn_id]
        if not sources:
            parts.append(f'<turn id={quoteattr(label)} evidence="none" />')
        for row in sources:
            attributes = {
                "id": row["source_id"], "turn": label, "turn_id": turn_id,
                "role": row["role"],
                "time": store.context_timestamp(float(row["created_at"])),
                "delivery": str(row.get("delivery_state") or "unknown"),
            }
            attrs = " ".join(f"{key}={quoteattr(str(value))}" for key, value in attributes.items())
            parts.append(f'<source {attrs}>{escape(str(row.get("content") or ""))}</source>')
    return "\n".join(parts)
