import logging
from typing import Any

from ..agenda_tools import AGENDA_TOOL_POLICY
from ..logging_context import log_event, safe_preview
from ..mcp_client import MCP_TOOL_POLICY
from ..memory_tools import MEMORY_TOOL_POLICY
from ..thinking_tools import THINKING_TOOL_POLICY
from .turn_support import (
    AGENDA_POLICY_TOOLS,
    HEARTBEAT_PROMPT_PATH,
    HEARTBEAT_SYSTEM_PROMPT,
    MEMORY_POLICY_TOOLS,
    THINKING_POLICY_TOOLS,
    REPLY_WAIT_PROMPT_PATH,
    REPLY_WAIT_SYSTEM_PROMPT,
    STYLE_CARD_PROMPT_PATH,
    STYLE_CARD_SYSTEM_PROMPT,
    SYSTEM_PROMPT_PATH,
    live_prompt as _live_prompt,
    sections as _sections,
)


logger = logging.getLogger(__name__)


class PromptRenderer:
    def _workspace_prompt_state(self) -> dict[str, str]:
        state = getattr(self, "_loaded_workspace_prompts", None)
        if state is None:
            state = {}
            self._loaded_workspace_prompts = state
        return state

    def _log_workspace_prompt(
        self,
        name: str,
        path: Any,
        text: str,
        *,
        optional: bool,
    ) -> None:
        fingerprint = f"{path}\0{text}"
        state = self._workspace_prompt_state()
        if state.get(name) == fingerprint:
            return
        state[name] = fingerprint
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
                preview=safe_preview(text, 160),
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
        path = getattr(self.config, "soul_prompt_path", None)
        fallback = str(getattr(self.config, "soul_prompt", "") or "")
        text = _live_prompt(path, fallback) if path is not None else fallback
        self._log_workspace_prompt("soul", path, text, optional=False)
        return text

    def _system(self) -> list[dict[str, Any]]:
        system_prompt = self.config.system_prompt
        soul_path = getattr(self.config, "soul_prompt_path", None)
        if soul_path is not None:
            system_prompt = _live_prompt(SYSTEM_PROMPT_PATH, system_prompt)
        soul_prompt = self._workspace_soul()
        text = (
            system_prompt.replace(
                "{{SOUL}}", soul_prompt or "No additional Soul is configured."
            )
            .replace(
                "{{STYLE_CARD}}",
                _live_prompt(STYLE_CARD_PROMPT_PATH, STYLE_CARD_SYSTEM_PROMPT),
            )
            .replace("{{CAPABILITY_POLICIES}}", "")
        )
        blocks: list[dict[str, Any]] = [
            {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}
        ]
        # Keep the catalog as its own cached system block so editing stickers does
        # not invalidate the large contract/Soul/style prefix.
        emotions = self.store.emotion_context()
        if emotions.strip():
            blocks.append(
                {
                    "type": "text",
                    "text": _sections(("emotion_catalog", emotions)),
                    "cache_control": {"type": "ephemeral"},
                }
            )
        return blocks

    def _system_with_tool_policies(
        self, system: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        names = {str(tool.get("name") or "") for tool in tools}
        policies: list[str] = []
        if names & AGENDA_POLICY_TOOLS:
            policies.append(AGENDA_TOOL_POLICY.strip())
        if names & MEMORY_POLICY_TOOLS:
            policies.append(MEMORY_TOOL_POLICY.strip())
        if names & THINKING_POLICY_TOOLS:
            policies.append(THINKING_TOOL_POLICY.strip())
        mcp_names = {str(tool.get("name") or "") for tool in self.mcp.tool_specs}
        if names & mcp_names:
            policies.append(MCP_TOOL_POLICY.strip())
        if not policies:
            return system
        return [
            *system,
            {
                "type": "text",
                "text": "# Available capability guidance\n\n"
                + "\n\n".join(policies),
            },
        ]

    def _workspace_heartbeat_guidance(self, *, log: bool = True) -> str:
        path = getattr(self.config, "heartbeat_prompt_path", None)
        text = (
            _live_prompt(path, "", optional=True)
            if path is not None
            else str(getattr(self.config, "heartbeat_prompt", "") or "")
        )
        if log:
            self._log_workspace_prompt("heartbeat", path, text, optional=True)
        return text

    def _heartbeat_system_prompt(self) -> str:
        prompt = _live_prompt(HEARTBEAT_PROMPT_PATH, HEARTBEAT_SYSTEM_PROMPT)
        workspace_prompt = self._workspace_heartbeat_guidance(log=False)
        if workspace_prompt:
            prompt += "\n\n# Workspace heartbeat guidance\n\n" + workspace_prompt
        return prompt

    def _reply_wait_system_prompt(self) -> str:
        return _live_prompt(REPLY_WAIT_PROMPT_PATH, REPLY_WAIT_SYSTEM_PROMPT)
