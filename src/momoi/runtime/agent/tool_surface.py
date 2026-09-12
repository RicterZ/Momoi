import copy
import json
import logging
from typing import Any, Callable

from ...tools.contracts.agenda import AGENDA_TOOL_SPECS
from ...tools.contracts.builtin import BUILTIN_TOOL_SPECS
from ...observability.events import TRACE, log_event
from ...tools.contracts.memory import MEMORY_TOOL_SPECS
from ...tools.contracts.thinking import THINKING_TOOL_SPECS
from ...storage import estimate_tokens
from ..tool_contracts.context import RECALL_TOOL_SPEC, heartbeat_begin_spec
from ..tool_contracts.current_state import current_state_finish_spec
from ..tool_contracts.conversation import (
    END_TURN_TOOL_SPEC, HEARTBEAT_ACTIVITY_TOOL_SPEC, GOAL_REVIEW_TOOL_SPEC, send_bubbles_tool_spec,
)
from ..tool_contracts.runtime import (
    READ_TOOL_RESULT_SPEC,
    tool_enable_spec,
)
from ..tool_contracts.plan import PLAN_TOOLS, PLAN_STEP_FINISH
from .progress import public_tool_spec, requires_owner_progress
from ..tool_contracts.voice import SEND_VOICE_TOOL_SPEC

logger = logging.getLogger("momoi.runtime.turns")


class ToolSurface:
    """Projects the tool catalog exposed to each workflow."""

    def __init__(self, mcp: Any, channels: dict[str, Any], *, voice_enabled: bool = False, exec_enabled: bool = False, emotion_catalog: Callable[[], bool] | None = None):
        self.mcp = mcp
        self.channel_names = list(channels)
        self.voice_enabled = voice_enabled
        self.emotion_catalog = emotion_catalog or (lambda: True)
        self.builtin_specs = [spec for spec in BUILTIN_TOOL_SPECS if exec_enabled or spec["name"] != "exec"]

    @staticmethod
    def public_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [public_tool_spec(spec) for spec in specs]

    def owner_progress_tool_names(self) -> frozenset[str]:
        specs = [*PLAN_TOOLS, *AGENDA_TOOL_SPECS, *self.builtin_specs, *self.mcp.tool_specs]
        return frozenset(
            str(spec.get("name") or "")
            for spec in specs
            if requires_owner_progress(spec)
        )

    def mcp_server_groups(self) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for spec in self.public_specs(
            sorted(self.mcp.tool_specs, key=lambda item: str(item.get("name") or ""))
        ):
            name = str(spec.get("name") or "")
            group = self.mcp.tool_group(name)
            groups.setdefault(group, []).append(spec)
        return dict(sorted(groups.items()))

    def mcp_group_description(self, group: str) -> str:
        configs = getattr(self.mcp, "configs", {})
        config = configs.get(group) if isinstance(configs, dict) else None
        description = (
            str(config.get("description") or "").strip()
            if isinstance(config, dict)
            else ""
        )
        return description or f"External MCP capabilities provided by {group}."

    @staticmethod
    def _schema_tokens(specs: list[dict[str, Any]]) -> int:
        return estimate_tokens(
            json.dumps(specs, ensure_ascii=False, separators=(",", ":"), default=str)
        )

    def _log_conversation_surface(self, tools: list[dict[str, Any]]) -> None:
        log_event(
            logger,
            TRACE,
            "conversation_tool_surface",
            tool_count=len(tools),
            tool_schema_tokens=self._schema_tokens(tools),
            tool_names=[str(spec.get("name") or "") for spec in tools],
        )

    @staticmethod
    def append_visible(
        tools: list[dict[str, Any]], specs: list[dict[str, Any]]
    ) -> list[str]:
        existing = {str(spec.get("name") or "") for spec in tools}
        added: list[str] = []
        for spec in specs:
            name = str(spec.get("name") or "")
            if not name or name in existing:
                continue
            tools.insert(max(0, len(tools) - 1), spec)
            existing.add(name)
            added.append(name)
        return added

    def conversation_specs(self) -> list[dict[str, Any]]:
        groups = self.mcp_server_groups()
        catalog = {
            group: self.mcp_group_description(group) for group in groups
        }
        tools = [
            copy.deepcopy(RECALL_TOOL_SPEC),
            heartbeat_begin_spec(catalog),
            copy.deepcopy(HEARTBEAT_ACTIVITY_TOOL_SPEC),
            copy.deepcopy(GOAL_REVIEW_TOOL_SPEC),
            self.send_bubbles_spec(),
            *([copy.deepcopy(SEND_VOICE_TOOL_SPEC)] if self.voice_enabled else []),
            READ_TOOL_RESULT_SPEC,
            *copy.deepcopy(MEMORY_TOOL_SPECS),
            *copy.deepcopy(THINKING_TOOL_SPECS),
            *self.public_specs(AGENDA_TOOL_SPECS),
            *self.public_specs(PLAN_TOOLS),
            copy.deepcopy(PLAN_STEP_FINISH),
            *self.public_specs(self.builtin_specs),
            *([tool_enable_spec(catalog)] if catalog else []),
            current_state_finish_spec(),
            copy.deepcopy(END_TURN_TOOL_SPEC),
        ]
        self._log_conversation_surface(tools)
        return tools

    def permitted_names(self, stage: str) -> frozenset[str]:
        external = {
            str(spec.get("name") or "")
            for spec in [*self.builtin_specs, *self.mcp.tool_specs]
        }
        agenda = {str(spec["name"]) for spec in AGENDA_TOOL_SPECS}
        memory = {str(spec["name"]) for spec in MEMORY_TOOL_SPECS}
        thinking = {str(spec["name"]) for spec in THINKING_TOOL_SPECS}
        shared = {"send_bubbles", "read_tool_result"}
        voice = {"send_voice"} if self.voice_enabled else set()
        shared.update(voice)
        general_chat = {
            "recall",
            "tool_enable",
            "end_turn",
            *shared,
            *agenda,
            *memory,
            *thinking,
            *external,
        }
        if stage == "owner":
            return frozenset(general_chat | {"plan_create", "plan_start", "plan_get", "plan_update", "plan_cancel"})
        if stage == "heartbeat":
            return frozenset(
                {
                    "heartbeat_begin",
                    "heartbeat_activity",
                    "end_turn",
                    *shared,
                    *agenda,
                    *memory,
                    *thinking,
                    *external,
                }
            )
        if stage == "webhook":
            return frozenset({"send_bubbles", "curl", "read_tool_result", "end_turn", *voice})
        if stage == "reply_followup":
            return frozenset(general_chat)
        if stage == "goal":
            return frozenset(
                {
                    "goal_review",
                    "goal_create",
                    "memory_search",
                    *voice,
                    "send_bubbles",
                    "read_tool_result",
                    "tool_enable",
                    "end_turn",
                    *external,
                }
            )
        raise ValueError(f"stage does not use the conversation tool surface: {stage}")

    def send_bubbles_spec(self) -> dict[str, Any]:
        return send_bubbles_tool_spec(
            self.channel_names, emotion_catalog=self.emotion_catalog()
        )
