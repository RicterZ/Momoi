import json
import uuid

from .context_service import ContextService
from .prompt_renderer import PromptRenderer
from .agent.loop import AgentLoop
from .turn_committer import TurnCommitter
from .workflows import (
    EpisodeWorkflow,
    GoalWorkflow,
    HeartbeatWorkflow,
    MemoryMaintenanceWorkflow,
    OwnerWorkflow,
    ReflectionWorkflow,
    WebhookWorkflow,
)


class TurnRunner(
    EpisodeWorkflow,
    WebhookWorkflow,
    MemoryMaintenanceWorkflow,
    GoalWorkflow,
    ReflectionWorkflow,
    HeartbeatWorkflow,
    OwnerWorkflow,
    ContextService,
    AgentLoop,
    PromptRenderer,
    TurnCommitter,
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
