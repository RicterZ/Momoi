import copy
import fnmatch
import json
import logging
from typing import Any

from ...agenda_tools import AGENDA_TOOL_SPECS
from ...builtin_tools import BUILTIN_TOOL_SPECS, SELF_DIRECTED_BUILTIN_TOOL_SPECS
from ...logging_context import TRACE, log_event
from ...memory_tools import MEMORY_TOOL_SPECS
from ...storage import estimate_tokens
from ..tool_contracts.context import RECALL_TOOL_SPEC, heartbeat_begin_spec
from ..tool_contracts.conversation import (
    owner_end_turn_tool_spec,
    send_bubbles_tool_spec,
)
from ..tool_contracts.runtime import (
    READ_TOOL_RESULT_SPEC,
    tool_enable_spec,
)
from .progress import decorate_tool_spec, public_tool_spec, requests_owner_progress

logger = logging.getLogger("momoi.runtime.turns")


class ToolSurface:
    """Projects the tool catalog exposed to each workflow."""

    def __init__(self, config: Any, mcp: Any, channels: dict[str, Any], primary: str):
        self.config = config
        self.mcp = mcp
        self.channel_names = list(channels)
        self.primary = primary

    @staticmethod
    def mcp_tool_group(name: str) -> str:
        parts = str(name).split("__", 2)
        return parts[1] if len(parts) == 3 else "other"

    @staticmethod
    def owner_progress_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            decorate_tool_spec(spec)
            if requests_owner_progress(spec)
            else public_tool_spec(spec)
            for spec in specs
        ]

    def mcp_server_groups(self) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for spec in self.owner_progress_specs(
            sorted(self.mcp.tool_specs, key=lambda item: str(item.get("name") or ""))
        ):
            group = self.mcp_tool_group(str(spec.get("name") or ""))
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

    def owner_internal_specs(self) -> list[dict[str, Any]]:
        return [
            READ_TOOL_RESULT_SPEC,
            *copy.deepcopy(MEMORY_TOOL_SPECS),
            *self.owner_progress_specs(AGENDA_TOOL_SPECS),
            *self.owner_progress_specs(BUILTIN_TOOL_SPECS),
        ]

    def owner_enable_groups(self) -> dict[str, list[dict[str, Any]]]:
        return self.mcp_server_groups()

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

    @staticmethod
    def _schema_tokens(specs: list[dict[str, Any]]) -> int:
        return estimate_tokens(
            json.dumps(specs, ensure_ascii=False, separators=(",", ":"), default=str)
        )

    def _log_owner_projection(
        self, *, visible: list[dict[str, Any]], full: list[dict[str, Any]]
    ) -> None:
        visible_tokens = self._schema_tokens(visible)
        full_tokens = self._schema_tokens(full)
        log_event(
            logger,
            TRACE,
            "tool_availability_projected",
            stage="owner",
            visible_tool_count=len(visible),
            full_tool_count=len(full),
            hidden_tool_count=max(0, len(full) - len(visible)),
            visible_tool_schema_tokens=visible_tokens,
            full_tool_schema_tokens=full_tokens,
            estimated_tool_schema_tokens_saved=max(0, full_tokens - visible_tokens),
            visible_tool_names=[str(spec.get("name") or "") for spec in visible],
        )

    def self_directed_mcp_groups(self) -> dict[str, list[dict[str, Any]]]:
        patterns = self.config.autonomy.allowed_tools
        groups: dict[str, list[dict[str, Any]]] = {}
        for spec in sorted(
            self.mcp.tool_specs, key=lambda item: str(item.get("name") or "")
        ):
            if not any(
                fnmatch.fnmatchcase(str(spec["name"]), pattern)
                for pattern in patterns
            ):
                continue
            server = self.mcp_tool_group(str(spec.get("name") or ""))
            groups.setdefault(server, []).append(spec)
        return dict(sorted(groups.items()))

    def heartbeat_external_specs(self) -> list[dict[str, Any]]:
        patterns = self.config.autonomy.allowed_tools
        internal = [
            READ_TOOL_RESULT_SPEC,
            *[
                spec
                for spec in SELF_DIRECTED_BUILTIN_TOOL_SPECS
                if any(
                    fnmatch.fnmatchcase(str(spec["name"]), pattern)
                    for pattern in patterns
                )
            ],
        ]
        groups = self.self_directed_mcp_groups()
        catalog = {
            server: self.mcp_group_description(server) for server in groups
        }
        return [heartbeat_begin_spec(catalog), *internal, tool_enable_spec(catalog)]

    def owner_specs(self, channel_name: str | None = None) -> list[dict[str, Any]]:
        mcp_groups = self.mcp_server_groups()
        internal = self.owner_internal_specs()
        catalog = {
            group: self.mcp_group_description(group) for group in mcp_groups
        }
        bubbles = self.send_bubbles_spec(channel_name)
        visible = [
            RECALL_TOOL_SPEC,
            bubbles,
            *internal,
            tool_enable_spec(catalog),
            owner_end_turn_tool_spec(),
        ]
        full = [
            RECALL_TOOL_SPEC,
            bubbles,
            *internal,
            tool_enable_spec(catalog),
            *[spec for specs in mcp_groups.values() for spec in specs],
            owner_end_turn_tool_spec(),
        ]
        self._log_owner_projection(visible=visible, full=full)
        return visible

    def self_directed_specs(self) -> list[dict[str, Any]]:
        patterns = self.config.autonomy.allowed_tools
        return [
            spec
            for spec in [*SELF_DIRECTED_BUILTIN_TOOL_SPECS, *self.mcp.tool_specs]
            if any(
                fnmatch.fnmatchcase(str(spec["name"]), pattern)
                for pattern in patterns
            )
        ]

    def send_bubbles_spec(self, channel_name: str | None = None) -> dict[str, Any]:
        return send_bubbles_tool_spec(
            self.channel_names, channel_name or self.primary
        )
