"""A private standard workflow following committed conversational end_turns."""

import asyncio
import copy
import logging
from datetime import datetime
from xml.sax.saxutils import quoteattr

from ...observability.events import log_event
from ...storage.current_state import StateConflict
from ..agent import AgentWorkflow
from ..context.current_state import pack_current_turn_context
from ..turn_support import PROMPT_ROOT, live_prompt


logger = logging.getLogger(__name__)
PROMPT_PATH = PROMPT_ROOT.joinpath("current_state.md")
MAINTENANCE_TIMEOUT_SECONDS = 30


class CurrentStateWorkflow:
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
        now = datetime.now(self.store.timezone).isoformat(timespec="seconds")
        latest = pack_current_turn_context(
            self.store,
            tasks[-1]["source_stage"],
            ("state_update_contract", live_prompt(PROMPT_PATH, "")),
            include_empty=True,
        )
        turns = "\n".join(
            f"<turn id={quoteattr(str(task['source_turn_id']))} "
            f"stage={quoteattr(str(task['source_stage']))} "
            f"committed_at={quoteattr(self.store.context_timestamp(task['committed_at']))} />"
            for task in tasks
        )
        request = f"<turns now={quoteattr(now)}>\n{turns}\n</turns>\n\n{latest}"
        first = tasks[0]
        messages = copy.deepcopy(first["messages"][: first["input_index"]])
        for task in tasks:
            messages.extend(copy.deepcopy(task["messages"][task["input_index"] :]))
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
            self.store.finish_current_state_batch(source_turn_ids, turn_id)
            complete = True
            return {"ok": True, "state": "completed"}

        workflow = AgentWorkflow(
            stage="current_state_maintenance",
            tool_names=frozenset({"current_state_finish"}),
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
