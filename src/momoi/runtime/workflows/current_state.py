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
        task = self.store.claim_current_state_task(source_turn_id)
        if task is None:
            return
        turn_id = task["turn_id"]
        try:
            await asyncio.wait_for(
                self._run_current_state_task(task),
                timeout=MAINTENANCE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            self.store.release_current_state_task(
                source_turn_id, turn_id, "cancelled", interrupted=True
            )
            raise
        except Exception as error:
            self.store.release_current_state_task(
                source_turn_id, turn_id, type(error).__name__
            )
            log_event(
                logger,
                logging.WARNING,
                "current_state_maintenance_failed",
                stage="current_state_maintenance",
                turn_id=turn_id,
                source_turn_id=source_turn_id,
                error_type=type(error).__name__,
            )
        else:
            log_event(
                logger,
                logging.INFO,
                "current_state_maintenance_complete",
                stage="current_state_maintenance",
                turn_id=turn_id,
                source_turn_id=source_turn_id,
            )
        finally:
            self.agenda_changed.set()

    async def _run_current_state_task(self, task):
        source_turn_id = task["source_turn_id"]
        turn_id = task["turn_id"]
        complete = False
        snapshot = self.store.current_state.snapshot()
        now = datetime.now(self.store.timezone).isoformat(timespec="seconds")
        latest = pack_current_turn_context(
            self.store,
            task["source_stage"],
            include_empty=True,
        )
        request = (
            live_prompt(PROMPT_PATH, "")
            + "\n\n"
            + f"<completed_turn stage={quoteattr(task['source_stage'])} "
            + f"input_message_index={quoteattr(str(task['input_index']))} "
            + f"committed_at={quoteattr(self.store.context_timestamp(task['committed_at']))} />\n"
            + f"<time now={quoteattr(now)} />\n"
            + latest
        )
        messages = copy.deepcopy(task["messages"])
        messages.append({"role": "user", "content": request})
        spec = {
            "name": "current_state_finish",
            "description": "Commit the private current-state change set and finish maintenance.",
            "input_schema": self.store.current_state.change_schema(),
        }

        async def execute_tool(call):
            nonlocal complete
            if not self.store.current_state_task_is_current(source_turn_id, turn_id):
                raise StateConflict("source_turn_no_longer_current")
            try:
                self.store.current_state.apply_arguments(
                    call.arguments,
                    source_turn_id=source_turn_id,
                    operation_id=f"current-state:{source_turn_id}",
                    expected_revision=snapshot.revision,
                )
            except StateConflict:
                # A fresh retry must read a fresh snapshot, not blindly overwrite it.
                raise
            except ValueError as error:
                return {"ok": False, "error": str(error)}
            self.store.finish_current_state_task(source_turn_id, turn_id)
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
            task["system"], messages, [spec], turn_id, workflow
        )
        if not complete:
            raise RuntimeError("state_maintenance_incomplete")
