import logging
from typing import Any

from ..tools.contracts.agenda import AGENDA_TOOL_POLICY
from ..tools.contracts.skills import SKILL_TOOL_POLICY
from ..observability.events import log_event
from ..mcp.prompt import MCP_TOOL_POLICY
from ..tools.contracts.memory import MEMORY_TOOL_POLICY
from ..tools.contracts.thinking import THINKING_TOOL_POLICY
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
            affinity = ""
            if getattr(self.config, "affinity_enabled", False):
                default_affinity = files("momoi").joinpath("prompts/affinity.md").read_text(encoding="utf-8")
                affinity_path = self.config.soul_prompt_path.parent / "AFFINITY.md" if self.config.soul_prompt_path else None
                affinity = _live_prompt(affinity_path, default_affinity, optional=True) if affinity_path else default_affinity
            soul_prompt = prompt.replace("{{AFFINITY}}", "").replace("{{SOUL}}", soul_prompt).rstrip()
            if affinity:
                soul_prompt += "\n\n" + affinity.strip()
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
            SKILL_TOOL_POLICY.strip(),
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
                "你的自然语言正文是内部思考，用户看不到，也不会执行动作。向用户发言、发送表情或戳一戳都必须调用 reply，由 Replyer 生成发送内容。"
                "先调用工具，再根据真实返回结果判断执行和投递状态；正文中的台词、动作描述、标签或自行写出的结果不代表已经执行。"
                "没有行动需要执行时，可以停止调用工具结束思考。\n\n"
                "未出现在当前工具参数列表中的能力，先通过 tool_search 查找，再调用 tool_enable 加载；加载后仍遵守当前阶段权限。\n\n"
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
