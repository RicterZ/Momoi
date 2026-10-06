import logging
from typing import Any

from ..tools.contracts.agenda import AGENDA_TOOL_POLICY
from ..observability.events import log_event
from ..mcp.prompt import MCP_TOOL_POLICY
from ..tools.contracts.memory import MEMORY_TOOL_POLICY
from ..tools.contracts.thinking import THINKING_TOOL_POLICY
from ..tools.contracts.images import IMAGE_TOOL_POLICY
from .turn_support import (
    AGENDA_POLICY_TOOLS,
    HEARTBEAT_PROMPT_PATH,
    HEARTBEAT_SYSTEM_PROMPT,
    MEMORY_POLICY_TOOLS,
    OWNER_PROMPT_PATH,
    OWNER_SYSTEM_PROMPT,
    THINKING_POLICY_TOOLS,
    REPLY_WAIT_PROMPT_PATH,
    REPLY_WAIT_SYSTEM_PROMPT,
    SYSTEM_PROMPT_PATH,
    live_prompt as _live_prompt,
    sections as _sections,
)


logger = logging.getLogger(__name__)


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
        """The operating contract, without the Soul.

        The Soul is delivered as its own leading block, so a contract template
        that embeds `{{SOUL}}` is split here and the placeholder dropped.
        """
        template = self.config.system_prompt
        if self.config.soul_prompt_path is not None:
            template = _live_prompt(SYSTEM_PROMPT_PATH, template)
        if "{{SOUL}}" not in template:
            return template.strip()
        head, _, tail = template.partition("{{SOUL}}")
        return "\n\n".join(part.strip() for part in (head, tail) if part.strip())

    def _system(self, *, planner: bool = False) -> list[dict[str, Any]]:
        soul_prompt = self._workspace_soul() or "未配置额外的 SOUL。"
        if planner:
            from importlib.resources import files
            default = files("momoi").joinpath("prompts/planner.md").read_text(encoding="utf-8")
            path = self.config.soul_prompt_path.parent / "PLANNER.md" if self.config.soul_prompt_path else None
            prompt = _live_prompt(path, default, optional=True) if path else default
            soul_prompt = prompt.replace("{{SOUL}}", soul_prompt)
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
        policies = [
            AGENDA_TOOL_POLICY.strip(), MEMORY_TOOL_POLICY.strip(),
            IMAGE_TOOL_POLICY.strip(), THINKING_TOOL_POLICY.strip(),
        ]
        if self.mcp.tool_specs:
            policies.append(MCP_TOOL_POLICY.strip())
        if not policies:
            return system
        return [
            *system,
            {
                "type": "text",
                "text": "# 可用能力使用指引\n\n"
                + "\n\n".join(policies),
            },
        ]

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

    def _heartbeat_system_prompt(self) -> str:
        prompt = _live_prompt(HEARTBEAT_PROMPT_PATH, HEARTBEAT_SYSTEM_PROMPT)
        workspace_prompt = self._workspace_heartbeat_guidance(log=False)
        return prompt.replace("{{HEARTBEAT}}", workspace_prompt)

    def _owner_system_prompt(self) -> str:
        return _live_prompt(OWNER_PROMPT_PATH, OWNER_SYSTEM_PROMPT)

    def _reply_wait_system_prompt(self) -> str:
        return _live_prompt(REPLY_WAIT_PROMPT_PATH, REPLY_WAIT_SYSTEM_PROMPT)
