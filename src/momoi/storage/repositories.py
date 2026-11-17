"""Compatibility entry points; implementations live in composed repositories."""
from __future__ import annotations
from typing import Any
from .contracts import RequestMetricRecord, MetricsPage

class RepositoryFacade:
    def subagent_task(self, task_id: str) -> dict[str, Any] | None:
        return self.subagents.subagent_task(task_id)

    def subagent_tasks(self, channel: str, *, limit: int=10) -> list[dict[str, Any]]:
        return self.subagents.subagent_tasks(channel, limit=limit)

    def create_subagent_task(self, source_turn_id: str, channel: str, title: str, task: str) -> dict[str, Any]:
        return self.subagents.create_subagent_task(source_turn_id, channel, title, task)

    def claim_subagent_task(self) -> dict[str, Any] | None:
        return self.subagents.claim_subagent_task()

    def attach_subagent_turn(self, task_id: str, turn_id: str) -> None:
        return self.subagents.attach_subagent_turn(task_id, turn_id)

    def add_subagent_report(self, task_id: str, progress: str) -> dict[str, Any]:
        return self.subagents.add_subagent_report(task_id, progress)

    def finish_subagent_task(self, task_id: str, outcome: str, summary: str, output_refs: list[str]) -> dict[str, Any] | None:
        return self.subagents.finish_subagent_task(task_id, outcome, summary, output_refs)

    def cancel_subagent_task(self, task_id: str, channel: str) -> dict[str, Any]:
        return self.subagents.cancel_subagent_task(task_id, channel)

    def cancel_subagent_tasks(self, channel: str | None=None) -> list[str]:
        return self.subagents.cancel_subagent_tasks(channel)

    def recover_subagent_tasks(self) -> None:
        return self.subagents.recover_subagent_tasks()

    def record_first_tool(self, turn_id: str, call_id: str, name: str) -> None:
        return self.request_metrics.record_first_tool(turn_id, call_id, name)

    def record_request_metric(self, record: RequestMetricRecord) -> None:
        return self.request_metrics.record_request_metric(record)

    def dashboard_request_metrics(self, *, hours: int=24, stage: str='', model: str='', before: int | None=None, limit: int=50) -> MetricsPage:
        plugin = self._usage_accounting
        return self.request_metrics.dashboard_request_metrics(
            hours=hours, stage=stage, model=model, before=before, limit=limit,
            estimate=None if plugin is None else plugin.estimate_cost)
