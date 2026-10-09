import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from ....models import ToolCall
from ....memory.writing.candidates import CandidateBudgetExceeded
from ....observability.events import log_event
from ...agent import AgentWorkflow
from ..memory_rendering import memory_record
from .contracts import MEMORY_OPERATION_FINISH_SPEC, MEMORY_OPERATION_SEARCH_SPEC
from .rendering import render_memory_operation_request
from .conversation import conversation_message

logger = logging.getLogger("momoi.runtime.turns")
PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "memory_operation.md"


class MemoryOperationWorkflow:
    async def _complete_memory_operation_turn(
        self, batch_id: str, stop: asyncio.Event
    ) -> None:
        if stop.is_set() or self.store.pending_events():
            return
        batch = self.store.claim_memory_operation(batch_id)
        if batch is None:
            return
        turn_id = str(batch["turn_id"])
        try:
            await self._run_memory_operation(batch)
        except asyncio.CancelledError:
            self.store.release_memory_operation(
                batch_id, turn_id, "cancelled", interrupted=True
            )
            raise
        except Exception as error:
            self.store.release_memory_operation(batch_id, turn_id, str(error))
            log_event(
                logger,
                logging.ERROR,
                "turn_failure",
                stage="memory_operation",
                turn_id=turn_id,
                error_type=type(error).__name__,
                exc_info=True,
            )
        finally:
            self.agenda_changed.set()

    async def _run_memory_operation(self, batch: dict[str, Any]) -> None:
        turn_id = batch["turn_id"]
        visible = {int(row["id"]): row for row in batch["context"]}
        current_ids = set(visible)
        for row in visible.values():
            current = self.store.memories.repository.active(row["kind"], row["key"], scope=row.get("meta", {}).get("scope", ""))
            if current is not None:
                current_ids.add(int(current["id"]))
        snapshots = self.memory.snapshots(sorted(current_ids))
        evidence = {event["event_id"]: event["text"] for event in batch["events"]}

        async def planner(context):
            snapshots, evidence = context.snapshots, context.evidence
            for item in self.store.memory_evidence_for_memories(
                list(snapshots)
            ):
                evidence[item["event_id"]] = item["content"]
            evidence_records = self.store.memory_operation_evidence_records(evidence)
            # Whole records with stable IDs, no global memory directory and no second foreground recall.
            now = time.time()
            request = render_memory_operation_request(
                now=now, timestamp=self.store.context_timestamp(now),
                operations=batch["operations"], visible=visible, snapshots=snapshots,
                evidence=evidence_records,
                goals=self.store.list_goals(), forgotten=context.forgotten,
                retrieval_fallback=context.retrieval_fallback,
            )
            complete = False
            completion: dict[str, Any] | None = None
            planned_arguments = None

            async def execute_tool(call: ToolCall) -> dict[str, Any]:
                nonlocal complete, completion, planned_arguments
                if call.name == "memory_operation_search":
                    query = call.arguments.get("query")
                    if (
                        set(call.arguments) != {"query"}
                        or not isinstance(query, str)
                        or not query.strip()
                        or len(query) > 240
                    ):
                        return {"ok": False, "error": "invalid_memory_operation_query"}
                    previous_ids = set(context.snapshots)
                    previous_forgotten = set(context.forgotten)
                    try:
                        await self.memory.writing.candidates.collect(context, [query])
                    except CandidateBudgetExceeded as error:
                        return {"ok": False, "error": str(error)}
                    related = {key: row for key, row in context.snapshots.items() if key not in previous_ids}
                    related_evidence = self.store.memory_evidence_for_memories(
                        list(related)
                    )
                    for item in related_evidence:
                        evidence[item["event_id"]] = item["content"]
                    return {
                        "ok": True,
                        "candidate_ids": sorted(context.snapshots),
                        "forgotten_memories": [row for key, row in context.forgotten.items()
                                               if key not in previous_forgotten],
                        "retrieval_fallback": context.retrieval_fallback,
                        "memories": [memory_record(row) for row in related.values()],
                        "owner_evidence": self.store.memory_operation_evidence_records(
                            {item["event_id"]: item["content"] for item in related_evidence}
                        ),
                    }
                try:
                    plan = self.memory.writing.review(context, call.arguments)
                    self.store.validate_memory_scopes(context.requests, plan.decisions)
                    planned_arguments = {"decisions": plan.decisions}
                except (TypeError, ValueError, KeyError) as error:
                    return {
                        "ok": False,
                        "error": "invalid_memory_operation_result",
                        "message": f"Correct the complete decision batch: {error}",
                    }
                complete = True
                completion = {"ok": True, "state": "planned", "decisions": len(plan.decisions)}
                return completion

            workflow = AgentWorkflow(
                stage="memory_operation",
                preserve_transcript=True,
                tool_names=frozenset(
                    {"memory_operation_finish", "memory_operation_search"}
                ),
                execute_tool=execute_tool,
                is_complete=lambda: complete,
                completion_result=lambda: completion,
                no_tool_correction="Use native tools. Submit every request outcome with memory_operation_finish alone; assistant text is not stored.",
            )
            # Private processing uses its own contract, not the role-play system or Soul.
            await self._run_agent_workflow(
                PROMPT_PATH.read_text(encoding="utf-8"),
                [
                    conversation_message(batch["conversation"]),
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": request,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                ],
                [MEMORY_OPERATION_SEARCH_SPEC, MEMORY_OPERATION_FINISH_SPEC],
                turn_id=turn_id,
                workflow=workflow,
            )
            if not complete:
                raise RuntimeError("memory operation ended without a decision")
            return planned_arguments

        plan = await self.memory.plan(
            batch["operations"], evidence=evidence, snapshots=snapshots, planner=planner,
            evidence_times={row["event_id"]: row["received_at"]
                            for row in self.store.memory_operation_evidence_records(evidence)},
        )
        self.store.apply_memory_operation(batch, plan)
        log_event(
            logger,
            logging.INFO,
            "turn_complete",
            stage="memory_operation",
            turn_id=turn_id,
            source_turn_id=batch["id"],
            operations=len(batch["operations"]),
            llm=self.store.turn_usage(turn_id),
        )
