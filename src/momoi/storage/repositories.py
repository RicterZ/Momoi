"""Compatibility entry points; implementations live in composed repositories."""
from __future__ import annotations
from typing import Any
from .memory.memory_values import format_memory
from .contracts import ActiveMemory, InventoryMemory, PlanCreateInput, PlanRecord, PlanStepInput, RequestMetricRecord, MetricsPage

class RepositoryFacade:
    def _memory_context(self, rows):
        return "\n\n".join(format_memory(dict(row)) for row in rows)


    def task_plan(self, plan_id: str) -> PlanRecord | None:
        return self.plans.task_plan(plan_id)

    def create_task_plan(self, args: PlanCreateInput, turn_id: str, channel: str) -> PlanRecord:
        return self.plans.create_task_plan(args, turn_id, channel)

    def update_task_plan(self, plan_id: str, channel: str, version: int, steps: list[PlanStepInput], request: str | None=None) -> PlanRecord:
        return self.plans.update_task_plan(plan_id, channel, version, steps, request)

    def pause_task_plan(self, plan_id: str, turn_id: str) -> PlanRecord | None:
        return self.plans.pause_task_plan(plan_id, turn_id)

    def paused_task_plans(self, channel: str) -> list[PlanRecord]:
        return self.plans.paused_task_plans(channel)

    def plan_resume_safety(self, plan: PlanRecord) -> str:
        return self.plans.plan_resume_safety(plan)

    def resume_task_plan(self, plan_id: str, channel: str, version: int, context: dict[str, Any], *, owner_feedback: dict[str, Any] | None=None) -> PlanRecord:
        return self.plans.resume_task_plan(plan_id, channel, version, context, owner_feedback=owner_feedback)

    def pause_task_plan_for_limit(self, plan_id: str, turn_id: str) -> None:
        return self.plans.pause_task_plan_for_limit(plan_id, turn_id)

    def save_plan_limit_handoff(self, plan_id: str, summary: str, output_refs: list[str]) -> None:
        return self.plans.save_plan_limit_handoff(plan_id, summary, output_refs)

    def cancel_task_plan(self, plan_id: str, channel: str) -> PlanRecord:
        return self.plans.cancel_task_plan(plan_id, channel)

    def submit_task_plan(self, plan_id: str, channel: str, version: int, summary: str, evidence: str, validation: str, turn_id: str) -> PlanRecord:
        return self.plans.submit_task_plan(plan_id, channel, version, summary, evidence, validation, turn_id)

    def start_task_plan(self, plan_id: str, channel: str, context: dict[str, Any] | None=None, *, version: int | None=None, approval: dict[str, Any] | None=None) -> PlanRecord:
        return self.plans.start_task_plan(plan_id, channel, context, version=version, approval=approval)

    def claim_task_plan(self) -> PlanRecord | None:
        return self.plans.claim_task_plan()

    def stop_task_plans(self, channel: str | None=None) -> None:
        return self.plans.stop_task_plans(channel)

    def recover_task_plans(self, plan_id: str | None=None) -> None:
        return self.plans.recover_task_plans(plan_id)

    def finish_task_plan_step(self, plan_id: str, turn_id: str, outcome: str, summary: str, output_refs: list[str], abort: bool=False) -> dict[str, Any]:
        return self.plans.finish_task_plan_step(plan_id, turn_id, outcome, summary, output_refs, abort)

    def maintenance_memory_inventory(self) -> list[InventoryMemory]:
        return self.memory.inventory()

    def purge_expired_memories(self, *, now: float | None=None) -> int:
        return self.memory.purge_expired(now=now)

    def always_memory_context(self) -> str:
        return self._memory_context(self.memory.rows("always"))

    def scoped_memory_context(self, scope: str) -> str:
        return self._memory_context(self.memory.rows("scoped", scope=scope))

    def has_memory(self, kind: str, key: str, *, scope: str = "") -> bool:
        return self.memory.has(kind, key, scope=scope)

    def active_memory(self, kind: str, key: str, *, scope: str = "") -> ActiveMemory | None:
        return self.memory.active(kind, key, scope=scope)

    def memory_snapshots(self, ids: list[int]) -> dict[int, dict[str, object]]:
        return self.memory.snapshots(ids)

    def _add_memory_evidence(self, memory_id, source_event_id, quote, now):
        self.memory.add_evidence(memory_id, source_event_id, quote, now)

    def record_first_tool(self, turn_id: str, call_id: str, name: str) -> None:
        return self.request_metrics.record_first_tool(turn_id, call_id, name)

    def record_request_metric(self, record: RequestMetricRecord) -> None:
        return self.request_metrics.record_request_metric(record)

    def dashboard_request_metrics(self, *, hours: int=24, stage: str='', model: str='', before: int | None=None, limit: int=50) -> MetricsPage:
        plugin = self._usage_accounting
        return self.request_metrics.dashboard_request_metrics(
            hours=hours, stage=stage, model=model, before=before, limit=limit,
            estimate=None if plugin is None else plugin.estimate_cost)
