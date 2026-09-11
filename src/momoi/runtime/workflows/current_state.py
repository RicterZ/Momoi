"""A private standard workflow following committed conversational end_turns."""

import asyncio
import copy
import json
import logging
from time import time

from ...models import IncomingMessage, TurnDraft
from ...observability.events import log_event
from ...storage.memory.current_state import StateConflict
from ..agent import AgentWorkflow
from ..context.current_state import pack_current_turn_context
from ..transcript.maintenance import maintenance_transcript
from ..transcript.rendering import render_pending_turns
from ..turn_support import PROMPT_ROOT, live_prompt, context_data_message


logger = logging.getLogger(__name__)
PROMPT_PATH = PROMPT_ROOT.joinpath("current_state.md")
MAINTENANCE_TIMEOUT_SECONDS = 30


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
        batch = self.store.claim_current_state_batch(source_turn_id)
        if batch is None:
            return
        turn_id = batch["turn_id"]
        source_turn_ids = batch["source_turn_ids"]
        try:
            await asyncio.wait_for(
                self._run_current_state_task(batch),
                timeout=MAINTENANCE_TIMEOUT_SECONDS,
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
            ("runtime_state", f"Current local time: {self.store.context_timestamp(time())}"),
            ("state_update_contract", live_prompt(PROMPT_PATH, "")),
            include_empty=True,
        )
        rows = {row["id"]: row for row in self._recent_conversation_rows()}
        rows.update({row["id"]: row for row in
                     self.store.conversation_messages_for_turns(source_turn_ids)})
        messages, turn_labels = maintenance_transcript(
            self.store, list(rows.values()), source_turn_ids,
        )
        injected = self.store.injected_memory_snapshots()
        events = self._source_turn_owner_events(source_turn_ids)
        # Snapshot the transcript before the tool loop mutates the live list;
        # a queued memory review must never capture this Turn's own tool rounds.
        draft = TurnDraft(memory_context=injected, memory_conversation=[*messages])
        context = context_data_message(
            ("long_term_memories", self.store._memory_context(list(injected.values()))),
        )
        if context:
            messages.insert(0, context)
        labels = [turn_labels[value] for value in source_turn_ids]
        request = render_pending_turns(labels) + "\n\n" + latest
        messages.append({"role": "user", "content": request})
        # Retain exact schemas and order, including enabled MCP tools. Tasks staged
        # before tool snapshots were introduced cannot recover their original surface.
        tools = copy.deepcopy(tasks[-1].get("tools"))
        if tools is None:
            tools = self.tool_surface.conversation_specs()

        async def execute_tool(call):
            nonlocal complete
            if not self.store.current_state_batch_is_current(
                source_turn_ids, turn_id
            ):
                raise StateConflict("source_turn_no_longer_current")
            if call.name == "memory_operation":
                return self.memory_tools.execute(call, events, draft)
            try:
                self.store.current_state.apply_arguments(
                    call.arguments,
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
            preserve_transcript=True,
        )
        await self._run_agent_workflow(
            tasks[-1]["system"], messages, tools, turn_id, workflow
        )
        if not complete:
            raise RuntimeError("state_maintenance_incomplete")
