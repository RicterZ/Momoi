import logging
import re
from typing import Any

from ..config.heartbeat_activities import (
    HeartbeatPlan, activities_path, activity_catalog, load_plan,
)
from ..tools.contracts.agenda import AGENDA_TOOL_POLICY
from ..observability.events import log_event
from ..tools.contracts.memory import MEMORY_TOOL_POLICY
from ..tools.contracts.images import IMAGE_TOOL_POLICY
from .turn_support import (
    HEARTBEAT_PROMPT_PATH,
    HEARTBEAT_SYSTEM_PROMPT,
    OWNER_PROMPT_PATH,
    OWNER_SYSTEM_PROMPT,
    SYSTEM_PROMPT_PATH,
    live_prompt as _live_prompt,
)


logger = logging.getLogger(__name__)
# Workspace prompts written for the old template still carry the placeholder;
# The persona is now injected after the Planner prompt instead.
_SOUL_PLACEHOLDER = re.compile(r"<soul>\s*\{\{SOUL\}\}\s*</soul>|\{\{SOUL\}\}")


def _strip_soul_placeholder(text: str) -> str:
    return _SOUL_PLACEHOLDER.sub("", text)


class PromptRenderer:
    def _log_workspace_prompt(
        self,
        name: str,
        path: Any,
        text: str,
        *,
        optional: bool,
    ) -> None:
        fingerprint = f"{path}\0{text}"
        if self._loaded_workspace_prompts.get(name) == fingerprint:
            return
        self._loaded_workspace_prompts[name] = fingerprint
        fields = {
            "name": name,
            "path": str(path) if path is not None else "",
            "chars": len(text),
        }
        if text:
            log_event(
                logger,
                logging.INFO,
                "workspace_prompt_loaded",
                **fields,
            )
            return
        log_event(
            logger,
            logging.INFO,
            "workspace_prompt_missing",
            optional=optional,
            **fields,
        )

    def _workspace_soul(self) -> str:
        path = self.config.soul_prompt_path
        fallback = self.config.soul_prompt
        text = _live_prompt(path, fallback) if path is not None else fallback
        self._log_workspace_prompt("soul", path, text, optional=False)
        return text

    def _contract(self) -> str:
        """The operating contract: the workspace SYSTEM.md, or the built-in rules."""
        path = self.config.soul_prompt_path.parent / "SYSTEM.md" if self.config.soul_prompt_path else None
        template = self.config.system_prompt
        if path is not None:
            template = _live_prompt(path, "", optional=True) or _live_prompt(SYSTEM_PROMPT_PATH, template)
        return _strip_soul_placeholder(template).strip()

    def _persona(self) -> str:
        soul_prompt = f"# 人格设定\n<soul>\n{self._workspace_soul() or '未配置额外的人格设定。'}\n</soul>"
        if getattr(getattr(self, "config", None), "affinity_enabled", False):
            from importlib.resources import files
            default_affinity = files("momoi").joinpath("prompts/affinity.md").read_text(encoding="utf-8")
            affinity_path = self.config.soul_prompt_path.parent / "AFFINITY.md" if self.config.soul_prompt_path else None
            affinity = _live_prompt(affinity_path, default_affinity, optional=True) if affinity_path else default_affinity
            soul_prompt += "\n\n" + affinity.strip()
        return soul_prompt

    def _workflow_system(self, rules: str) -> list[dict[str, Any]]:
        """Persona plus one private workflow's rules, without the Planner contract."""
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}
                for text in (self._persona(), rules.strip()) if text]

    def _system(self, *, planner: bool = False) -> list[dict[str, Any]]:
        soul_prompt = self._persona()
        if planner:
            from importlib.resources import files
            default = files("momoi").joinpath("prompts/planner.md").read_text(encoding="utf-8")
            path = self.config.soul_prompt_path.parent / "PLANNER.md" if self.config.soul_prompt_path else None
            prompt = _live_prompt(path, default, optional=True) if path else default
            prompt = _strip_soul_placeholder(prompt.replace("{{AFFINITY}}", "")).strip()
            soul_prompt = "\n\n".join(part for part in (prompt, soul_prompt) if part)
        blocks: list[dict[str, Any]] = []
        # Identity first, operating contract second: the rules then sit closest to
        # the conversation, where their influence on the next step is strongest.
        # Both are stable, so both stay inside the cached prefix.
        for text in (soul_prompt, self._contract()):
            if text:
                blocks.append(
                    {
                        "type": "text",
                        "text": text,
                        "cache_control": {"type": "ephemeral"},
                    }
                )
        surface = getattr(self, "tool_surface", None)
        index = surface.tool_index() if surface is not None else ""
        if index:
            blocks.append({"type": "text", "text": index, "cache_control": {"type": "ephemeral"}})
        return blocks

    def _system_with_tool_policies(
        self, system: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        # Public capability guidance is identical across execution stages and
        # does not change when an external MCP group becomes visible.
        policies = [AGENDA_TOOL_POLICY.strip(), MEMORY_TOOL_POLICY.strip(), IMAGE_TOOL_POLICY.strip()]
        return [*system, {"type": "text", "text": "# 可用能力使用指引\n\n" + "\n\n".join(policies)}]

    def _workspace_heartbeat_guidance(self, *, log: bool = True) -> str:
        path = self.config.heartbeat_prompt_path
        text = (
            _live_prompt(path, "", optional=True)
            if path is not None
            else ""
        )
        if log:
            self._log_workspace_prompt("heartbeat", path, text, optional=True)
        return text

    def _heartbeat_plan(self) -> HeartbeatPlan:
        path = self.config.heartbeat_prompt_path
        if path is None:
            return HeartbeatPlan()
        try:
            return load_plan(activities_path(path))
        except (OSError, ValueError) as error:
            log_event(logger, logging.WARNING, "heartbeat_activities_invalid",
                      path=str(activities_path(path)), error=str(error))
            return HeartbeatPlan()

    def _heartbeat_system_prompt(self, plan: HeartbeatPlan | None = None) -> str:
        prompt = _live_prompt(HEARTBEAT_PROMPT_PATH, HEARTBEAT_SYSTEM_PROMPT)
        workspace_prompt = self._workspace_heartbeat_guidance(log=False)
        catalog = activity_catalog(self._heartbeat_plan() if plan is None else plan)
        return prompt.replace("{{HEARTBEAT}}", workspace_prompt).replace("{{HEARTBEAT_ACTIVITIES}}", catalog)

    def _owner_system_prompt(self) -> str:
        return _live_prompt(OWNER_PROMPT_PATH, OWNER_SYSTEM_PROMPT)
