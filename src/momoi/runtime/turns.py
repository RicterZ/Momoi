import json
import uuid

from .context.service import ContextService
from .prompt_renderer import PromptRenderer
from .workflows.current_state import CurrentStateWorkflow
from .agent.loop import AgentLoop
from .workflows import (
    EpisodeAnnealingWorkflow,
    EpisodeConsolidationWorkflow,
    GoalWorkflow,
    HeartbeatWorkflow,
    MemoryMaintenanceWorkflow,
    MemoryOperationWorkflow,
    OwnerWorkflow,
    ReflectionWorkflow,
    ReplyFollowupWorkflow,
    WebhookWorkflow,
)


from .workflows.plan import PlanWorkflow


class TurnRunner(
    PlanWorkflow,
    CurrentStateWorkflow,
    EpisodeAnnealingWorkflow,
    EpisodeConsolidationWorkflow,
    WebhookWorkflow,
    MemoryMaintenanceWorkflow,
    MemoryOperationWorkflow,
    GoalWorkflow,
    ReflectionWorkflow,
    HeartbeatWorkflow,
    ReplyFollowupWorkflow,
    OwnerWorkflow,
    ContextService,
    AgentLoop,
    PromptRenderer,
):
    @staticmethod
    def _turn_id(*parts: object) -> str:
        seed = json.dumps(
            parts,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        return uuid.uuid5(uuid.NAMESPACE_URL, f"momoi:{seed}").hex
