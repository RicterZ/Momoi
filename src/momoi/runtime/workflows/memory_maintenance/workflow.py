import asyncio
import logging
import time
from typing import Any

from ....logging_context import log_event, safe_preview
from ....models import ToolCall
from ...agent import AgentWorkflow, WorkflowProtocolError
from ...turn_support import MEMORY_MAINTENANCE_SYSTEM_PROMPT
from .contracts import MEMORY_MAINTENANCE_FINISH_SPEC
from .grouping import build_atomic_memory_groups, pack_memory_groups
from .parsing import parse_memory_maintenance_result
from .rendering import render_memory_maintenance_request
from .selection import (
    filter_owner_evidence_for_memories,
    select_daily_memory_seed_ids,
)

logger = logging.getLogger("momoi.runtime.turns")


class MemoryMaintenanceWorkflow:
    async def _complete_memory_maintenance_turn(
        self, turn_id: str, stop: asyncio.Event
    ) -> bool:
        """Run one durable batch and report whether it should be requeued."""
        if stop.is_set() or not self.store.claim_memory_maintenance_turn(turn_id):
            return False
        try:
            (
                completed,
                batch_changes,
                defer_reason,
            ) = await self._run_memory_maintenance_batch(turn_id)
            if defer_reason:
                self.store.release_memory_maintenance_turn(turn_id, defer_reason)
                log_event(
                    logger,
                    logging.WARNING,
                    "memory_maintenance_protocol_deferred",
                    stage="memory_maintenance",
                    turn_id=turn_id,
                    reason=defer_reason,
                )
                self.agenda_changed.set()
                return False
            if completed:
                journal = self.store.memory_maintenance_journal(turn_id)
                batch_items = [
                    item
                    for item in journal
                    if item.get("item_type") == "memory_maintenance_batch"
                ]
                self.store.complete_background_turn(turn_id)
                log_event(
                    logger,
                    logging.INFO,
                    "turn_complete",
                    stage="memory_maintenance",
                    turn_id=turn_id,
                    batches=len(batch_items),
                    changes=sum(
                        int(item.get("change_count") or 0) for item in batch_items
                    ),
                )
                return False
            self.store.release_memory_maintenance_turn(turn_id, None)
            log_event(
                logger,
                logging.DEBUG,
                "memory_maintenance_batch_yielded",
                stage="memory_maintenance",
                turn_id=turn_id,
                changes=batch_changes,
            )
            return not stop.is_set()
        except asyncio.CancelledError:
            self.store.release_memory_maintenance_turn(
                turn_id, "owner_stop" if self._stop_requested else "cancelled"
            )
            raise
        except Exception as error:
            self.store.release_memory_maintenance_turn(turn_id, type(error).__name__)
            log_event(
                logger,
                logging.ERROR,
                "turn_failure",
                stage="memory_maintenance",
                turn_id=turn_id,
                error_type=type(error).__name__,
                exc_info=True,
            )
            self.agenda_changed.set()
            return False

    async def _run_memory_maintenance_batch(
        self, turn_id: str
    ) -> tuple[bool, int, str | None]:
        journal = self.store.memory_maintenance_journal(turn_id)
        plan = next(
            (
                item
                for item in journal
                if item.get("item_type") == "memory_maintenance_plan"
            ),
            None,
        )
        if plan is None:
            previous = self.store.latest_memory_maintenance_completion() or {}
            snapshot_at = time.time()
            evidence_through = self.store.latest_owner_event_marker(through=snapshot_at)
            plan = {
                "mode": (
                    "delta"
                    if self.store.memory_maintenance_bootstrap_complete()
                    else "bootstrap"
                ),
                "snapshot_at": snapshot_at,
                "memory_after": float(previous.get("snapshot_at") or 0),
                "evidence_after_at": float(previous.get("evidence_through_at") or 0),
                "evidence_after_id": str(previous.get("evidence_through_id") or ""),
                "evidence_through_at": evidence_through[0],
                "evidence_through_id": evidence_through[1],
                "source_ids": self.store.memory_maintenance_source_ids(turn_id),
            }
            self.store.append_turn_journal(
                turn_id,
                "memory_maintenance_plan",
                plan,
            )
            journal = self.store.memory_maintenance_journal(turn_id)

        inventory = self.store.maintenance_memory_inventory()
        by_id = {int(item["id"]): item for item in inventory}
        evidence_through = (
            float(plan.get("evidence_through_at") or 0),
            str(plan.get("evidence_through_id") or ""),
        )
        evidence = self.store.memory_maintenance_owner_evidence(
            after_at=float(plan.get("evidence_after_at") or 0),
            after_id=str(plan.get("evidence_after_id") or ""),
            through_at=evidence_through[0],
            through_id=evidence_through[1],
        )
        if plan.get("mode") == "bootstrap":
            seed_ids = set(by_id)
        else:
            changed_ids = self.store.memory_maintenance_changed_ids(
                after=float(plan.get("memory_after") or 0),
                through=float(plan.get("snapshot_at") or time.time()),
            )
            seed_ids = select_daily_memory_seed_ids(inventory, evidence, changed_ids)

        completed_ids: set[int] = set()
        forced_groups: list[list[int]] = []
        for item in journal:
            if item.get("item_type") != "memory_maintenance_batch":
                continue
            completed_ids.update(
                int(memory_id)
                for memory_id in item.get("completed_ids", item.get("reviewed_ids", []))
            )
            for request in item.get("regroup_requests", []):
                if not isinstance(request, dict):
                    continue
                group = [
                    *request.get("anchor_ids", []),
                    *request.get("include_ids", []),
                ]
                if group:
                    forced_groups.append([int(memory_id) for memory_id in group])
        forced_groups = [
            group for group in forced_groups if not set(group).issubset(completed_ids)
        ]
        forced_ids = {memory_id for group in forced_groups for memory_id in group}
        completed_ids -= forced_ids
        seed_ids = (seed_ids - completed_ids) | forced_ids

        groups = build_atomic_memory_groups(
            inventory,
            seed_ids,
            forced_groups=forced_groups,
        )
        if not groups:
            self.store.append_turn_journal(
                turn_id,
                "memory_maintenance_complete",
                {
                    "mode": str(plan.get("mode") or "delta"),
                    "snapshot_at": float(plan.get("snapshot_at") or time.time()),
                    "evidence_through_at": evidence_through[0],
                    "evidence_through_id": evidence_through[1],
                },
            )
            return True, 0, None

        batches = pack_memory_groups(
            groups,
            by_id,
            max(1000, min(12000, self.config.max_input_tokens // 4)),
        )
        mutable_ids = batches[0]
        mutable = {memory_id: by_id[memory_id] for memory_id in mutable_ids}
        evidence_by_event = {
            str(item["event_id"]): item
            for item in filter_owner_evidence_for_memories(
                evidence, list(mutable.values())
            )
        }
        for item in self.store.memory_maintenance_evidence_for_memories(mutable_ids):
            evidence_by_event.setdefault(str(item["event_id"]), item)
        evidence = list(evidence_by_event.values())
        context = [
            item
            for item in inventory
            if item["activation"] == "always" and int(item["id"]) not in mutable
        ][:16]
        request = render_memory_maintenance_request(
            mutable_memories=list(mutable.values()),
            context_memories=context,
            memory_directory=inventory,
            owner_evidence=evidence,
            topic_context="",
        )
        owner_marker = self.store.latest_owner_event_marker()
        messages: list[dict[str, Any]] = [{"role": "user", "content": request}]
        evidence_by_id = {
            str(item["event_id"]): str(item["content"]) for item in evidence
        }
        decision: dict[str, Any] | None = None
        workflow_complete = False
        workflow_result: dict[str, object] | None = None

        async def execute_tool(call: ToolCall) -> dict[str, Any]:
            nonlocal decision, workflow_complete, workflow_result
            if workflow_complete:
                return {
                    "ok": False,
                    "error": "memory_maintenance_batch_already_completed",
                }
            parsed, error = parse_memory_maintenance_result(
                call.arguments,
                mutable_memories=mutable,
                context_ids={int(item["id"]) for item in context},
                directory_ids=set(by_id),
                owner_evidence=evidence_by_id,
            )
            if parsed is None:
                return {
                    "ok": False,
                    "error": "invalid_memory_maintenance_result",
                    "message": (
                        "Fix this exact validation error and resubmit the complete "
                        "batch: " + (error or "invalid_memory_maintenance_result")
                    ),
                }
            try:
                self.store.apply_memory_maintenance_batch(
                    turn_id,
                    parsed,
                    mutable,
                    owner_marker=owner_marker,
                )
            except ValueError as error:
                return {
                    "ok": False,
                    "error": "memory_maintenance_store_rejected",
                    "message": str(error),
                }
            decision = parsed
            workflow_complete = True
            workflow_result = {
                "ok": True,
                "changes": len(parsed["changes"]),
            }
            return {"ok": True, "state": "completed", **workflow_result}

        workflow = AgentWorkflow(
            stage="memory_maintenance",
            tool_names=frozenset({"memory_maintenance_finish"}),
            execute_tool=execute_tool,
            is_complete=lambda: workflow_complete,
            completion_result=lambda: workflow_result,
            no_tool_correction=(
                "[Trusted runtime protocol error. Plain assistant text is not stored. "
                "Call memory_maintenance_finish with the complete batch result.]"
            ),
        )
        try:
            result = await self._run_agent_workflow(
                MEMORY_MAINTENANCE_SYSTEM_PROMPT,
                messages,
                [MEMORY_MAINTENANCE_FINISH_SPEC],
                turn_id=turn_id,
                workflow=workflow,
            )
        except WorkflowProtocolError as error:
            return False, 0, str(error)
        if not isinstance(result, dict) or decision is None or not workflow_complete:
            return False, 0, "memory maintenance ended before completion"
        log_event(
            logger,
            logging.INFO,
            "memory_maintenance_applied",
            stage="memory_maintenance",
            turn_id=turn_id,
            mutable_ids=sorted(mutable),
            reviewed_ids=decision["reviewed_ids"],
            changes=safe_preview(decision["changes"], 6000),
            regroup_requests=decision["regroup_requests"],
            summary=safe_preview(decision["summary"], 500),
        )
        return False, len(decision["changes"]), None
