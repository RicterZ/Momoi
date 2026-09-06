import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from ....models import ToolCall
from ....observability.events import log_event
from ....storage import MemoryRecallQuery
from ...agent import AgentWorkflow
from .contracts import MEMORY_OPERATION_FINISH_SPEC, MEMORY_OPERATION_SEARCH_SPEC
from .parsing import parse_decisions

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
            current = self.store.active_memory(row["kind"], row["key"])
            if current is not None:
                current_ids.add(int(current["id"]))
        snapshots = self.store.memory_snapshots(sorted(current_ids))
        evidence = {event["event_id"]: event["text"] for event in batch["events"]}
        for item in self.store.memory_maintenance_evidence_for_memories(
            list(snapshots)
        ):
            evidence[item["event_id"]] = item["content"]
        evidence_records = self.store.memory_operation_evidence_records(evidence)
        # Whole records with stable IDs, no global memory directory and no second foreground recall.
        request = {
            "current_time": self.store.context_timestamp(time.time()),
            "current_unix_time": time.time(),
            "max_recent_ttl_hours": self.memory_tools.policy.recent_max_ttl_hours,
            "operation_requests": batch["operations"],
            "visible_memory_ids": sorted(visible),
            "outdated_visible_snapshots": [
                row for key, row in visible.items() if snapshots.get(key) != row
            ],
            "current_memories": list(snapshots.values()),
            "owner_evidence": evidence_records,
            "conversation_context": batch["conversation"],
        }
        complete = False
        completion: dict[str, Any] | None = None

        async def execute_tool(call: ToolCall) -> dict[str, Any]:
            nonlocal complete, completion
            if call.name == "memory_operation_search":
                query = call.arguments.get("query")
                if (
                    set(call.arguments) != {"query"}
                    or not isinstance(query, str)
                    or not query.strip()
                    or len(query) > 240
                ):
                    return {"ok": False, "error": "invalid_memory_operation_query"}
                dense = await self.semantic_recall.prepare(
                    [MemoryRecallQuery(query)], include_episode=False, output_limit=12
                )
                matches = self.store.search_memories(query, 12)
                matches += [
                    item
                    for item in self.store.rank_recalled_memories(
                        [MemoryRecallQuery(query)], 6, dense_evidence=dense
                    )
                    if item["source"] == "confirmed"
                ]
                related = self.store.memory_snapshots(
                    sorted({int(item["id"]) for item in matches})
                )
                snapshots.update(related)
                related_evidence = self.store.memory_maintenance_evidence_for_memories(
                    list(related)
                )
                for item in related_evidence:
                    evidence[item["event_id"]] = item["content"]
                return {
                    "ok": True,
                    "memories": list(related.values()),
                    "owner_evidence": self.store.memory_operation_evidence_records(
                        {item["event_id"]: item["content"] for item in related_evidence}
                    ),
                }
            try:
                decisions = parse_decisions(
                    call.arguments,
                    batch["operations"],
                    snapshots,
                    evidence,
                    self.memory_tools.policy.recent_max_ttl_hours,
                )
                self.store.apply_memory_operation(batch, decisions, snapshots)
            except (TypeError, ValueError, KeyError) as error:
                return {
                    "ok": False,
                    "error": "invalid_memory_operation_result",
                    "message": f"Correct the complete decision batch: {error}",
                }
            complete = True
            completion = {"ok": True, "decisions": len(decisions)}
            return completion

        workflow = AgentWorkflow(
            stage="memory_operation",
            tool_names=frozenset(
                {"memory_operation_finish", "memory_operation_search"}
            ),
            execute_tool=execute_tool,
            is_complete=lambda: complete,
            completion_result=lambda: completion,
            no_tool_correction="Use native tools. Submit every request outcome with memory_operation_finish alone; assistant text is not stored.",
        )
        # Private processing uses its own contract, not the companion Soul or Style Card.
        await self._run_agent_workflow(
            PROMPT_PATH.read_text(),
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(request, ensure_ascii=False),
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
