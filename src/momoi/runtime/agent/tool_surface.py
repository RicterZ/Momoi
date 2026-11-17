import copy
import json
import logging
from typing import Any, Callable

from ...tools.contracts.agenda import AGENDA_TOOL_SPECS
from ...tools.contracts.builtin import BASH_REPLACED_TOOLS, BUILTIN_TOOL_SPECS, builtin_tool_enabled
from ...observability.events import TRACE, log_event
from ...tools.contracts.memory import MEMORY_TOOL_SPECS
from ...tools.contracts.thinking import THINKING_TOOL_SPECS
from ...tools.contracts.images import IMAGE_TOOL_SPECS
from ...memory.text import estimate_tokens
from ..tool_contracts.context import RECALL_TOOL_SPEC, heartbeat_begin_spec
from ..tool_contracts.episode_relations import EPISODE_RELATIONS_TOOL_SPEC
from ..tool_contracts.current_state import current_state_finish_spec
from ..tool_contracts.conversation import (
    HEARTBEAT_ACTIVITY_TOOL_SPEC, GOAL_REVIEW_TOOL_SPEC,
)
from ..tool_contracts.runtime import (
    READ_TOOL_RESULT_SPEC, MCP_RELOAD_SPEC, WAIT_TOOL_SPEC,
    tool_search_spec,
    TOOL_ENABLE_SPEC,
)
from ..tool_contracts.subagent import PLANNER_SUBAGENT_TOOLS, WORKER_SUBAGENT_TOOLS
from ..tool_contracts.affinity import AFFINITY_TOOLS
from .progress import public_tool_spec
from ..tool_contracts.reply import REPLY_TOOL_SPEC
from ..tool_contracts.qq_call import QQ_CALL_STATUS_SPEC
from ..tool_contracts.qq_message import QQ_RECALL_MESSAGE_SPEC

PHONE_RESULT_SPEC = {
    "name": "phone_result",
    "description": "电话后台执行者向电话 Replyer 提交真实操作结果或失败原因；纯聊天 result 留空。非电话边车阶段不使用。",
    "input_schema": {"type": "object", "properties": {
        "result": {"type": "string", "maxLength": 6000}},
        "required": ["result"], "additionalProperties": False},
}

logger = logging.getLogger("momoi.runtime.turns")

DEFERRED_TOOLS = frozenset({
    "thinking_search", "thinking_read", "episode_relations", "memory_search",
    "write_file", "makedirs", "move_file", "delete_file",
    "skill_install", "skill_uninstall",
})
# The Planner reads and writes files but never executes commands; that belongs to the Worker.
PLANNER_BUILTINS = BASH_REPLACED_TOOLS
BUILTIN_GROUP_DESCRIPTIONS = {
    "builtin_skill_management": "安装或卸载工作区 skills 中的 Skill 工作流。",
    "builtin_mcp_management": "重载 MCP 工具服务连接，应用安装或配置变更并检查可用工具。",
    "builtin_files": "写入、修改、移动、删除文件和创建目录。",
}


class ToolSurface:
    """Projects the tool catalog exposed to each workflow."""

    def __init__(self, mcp: Any, channels: dict[str, Any], *, voice_enabled: bool = False, exec_enabled: bool = False, deepseek_web_enabled: bool = False, emotion_catalog: Callable[[], bool] | None = None, store: Any = None, skills: Any = None, affinity_enabled: Callable[[], bool] | None = None):
        self.affinity_enabled = affinity_enabled or (lambda: False)
        self.mcp = mcp
        self.store = store
        self.skills = skills
        self.channel_names = list(channels)
        self.voice_channels = [name for name, channel in channels.items()
                               if callable(getattr(channel, "send_voice", None))]
        self.voice_enabled = voice_enabled and bool(self.voice_channels)
        self.emotion_catalog = emotion_catalog or (lambda: True)
        self.builtin_specs = [
            spec for spec in BUILTIN_TOOL_SPECS
            if builtin_tool_enabled(spec["name"], exec_enabled=exec_enabled, deepseek_web_enabled=deepseek_web_enabled)
        ]

    @staticmethod
    def public_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [public_tool_spec(spec) for spec in specs]

    def mcp_server_groups(self) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for spec in self.public_specs(
            sorted(self.mcp.tool_specs, key=lambda item: str(item.get("name") or ""))
        ):
            name = str(spec.get("name") or "")
            group = self.mcp.tool_group(name)
            groups.setdefault(group, []).append(spec)
        return dict(sorted(groups.items()))

    def planner_resident_specs(self) -> list[dict[str, Any]]:
        history = [spec for spec in [*MEMORY_TOOL_SPECS, *THINKING_TOOL_SPECS, EPISODE_RELATIONS_TOOL_SPEC]
                   if spec["name"] in DEFERRED_TOOLS]
        if "onebot" in self.channel_names:
            history.append(QQ_RECALL_MESSAGE_SPEC)
        if "qq_call" in self.channel_names:
            history.append(QQ_CALL_STATUS_SPEC)
        return self.public_specs(history)

    def discovery_groups(self, role: str = "planner") -> dict[str, list[dict[str, Any]]]:
        """Deferred tools a role may load; the Planner's whole surface is resident."""
        if role == "planner":
            return {}
        groups = self.mcp_server_groups()
        if getattr(self.mcp, "path", None) is not None:
            groups["builtin_mcp_management"] = self.public_specs([MCP_RELOAD_SPEC])
        groups["builtin_files"] = self.public_specs([
            spec for spec in self.builtin_specs if spec["name"] in DEFERRED_TOOLS
            and spec["name"] not in {"skill_install", "skill_uninstall"}
        ])
        groups["builtin_skill_management"] = self.public_specs([
            spec for spec in self.builtin_specs if spec["name"] in {"skill_install", "skill_uninstall"}
        ])
        return groups

    def mcp_group_description(self, group: str) -> str:
        if group in BUILTIN_GROUP_DESCRIPTIONS:
            return BUILTIN_GROUP_DESCRIPTIONS[group]
        configs = getattr(self.mcp, "configs", {})
        config = configs.get(group) if isinstance(configs, dict) else None
        description = (
            str(config.get("description") or "").strip()
            if isinstance(config, dict)
            else ""
        )
        return description or f"{group} 提供的外部 MCP 工具。"

    def tool_index(self, role: str = "planner") -> str:
        groups = self.discovery_groups(role)
        sections = []
        if groups:
            sections.append("工具索引：\n" + "\n".join(
                f"- {group}: {self.mcp_group_description(group)}" for group in groups
            ) + "\n通过 tool_search 查找候选名称与描述，再用 tool_enable 批量加载选定工具。")
        skills = self.skills.catalog() if self.skills is not None else []
        if role != "planner" and skills:
            sections.append("Skill 索引：\n" + "\n".join(
                f"- {item['name']}: {item['description']}" for item in skills
            ) + "\n通过 skill_load 按名称加载适用的完整指引。")
        return "\n\n".join(sections)

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
        reply_spec = copy.deepcopy(REPLY_TOOL_SPEC)
        reply_spec["input_schema"]["properties"]["channel"] = {
            "type": "string", "enum": self.channel_names,
            "description": "目标渠道；省略时回复当前消息所在渠道。",
        }
        if "onebot" in self.channel_names:
            reply_spec["description"] += " QQ 文字回复中，Replyer 也能选择戳一戳用户；需要这种互动时在 intent 中说明意图。"
        if not self.voice_enabled:
            reply_spec["input_schema"]["properties"]["mode"]["enum"] = ["text"]
        tools = [
            copy.deepcopy(PHONE_RESULT_SPEC),
            copy.deepcopy(RECALL_TOOL_SPEC),
            copy.deepcopy(WAIT_TOOL_SPEC),
            *(copy.deepcopy(AFFINITY_TOOLS) if self.affinity_enabled() else []),
            heartbeat_begin_spec(),
            copy.deepcopy(HEARTBEAT_ACTIVITY_TOOL_SPEC),
            copy.deepcopy(GOAL_REVIEW_TOOL_SPEC),
            {**reply_spec, "description": reply_spec["description"] + (" 已配置语音合成；语音渠道：" + "、".join(self.voice_channels) + "。在这些渠道可选 mode=voice，其他渠道使用 mode=text。" if self.voice_enabled else " 当前仅支持 mode=text，语音合成或渠道语音能力不可用。")},
            READ_TOOL_RESULT_SPEC,
            *copy.deepcopy([spec for spec in MEMORY_TOOL_SPECS if spec["name"] not in DEFERRED_TOOLS]),
            *copy.deepcopy(IMAGE_TOOL_SPECS),
            *self.public_specs(AGENDA_TOOL_SPECS),
            *copy.deepcopy(PLANNER_SUBAGENT_TOOLS),
            *self.public_specs([spec for spec in BUILTIN_TOOL_SPECS if spec["name"] in PLANNER_BUILTINS]),
            *self.planner_resident_specs(),
            current_state_finish_spec(),
        ]
        self._log_conversation_surface(tools)
        return tools

    def worker_specs(self) -> list[dict[str, Any]]:
        groups = self.discovery_groups("worker")
        return [
            READ_TOOL_RESULT_SPEC,
            *copy.deepcopy(IMAGE_TOOL_SPECS[:1]),
            *self.public_specs([spec for spec in self.builtin_specs if spec["name"] not in DEFERRED_TOOLS]),
            *[
                spec for specs in groups.values() for spec in specs
                if spec.get("name") == "mcp__brave-search__brave_web_search"
            ],
            *([tool_search_spec(), copy.deepcopy(TOOL_ENABLE_SPEC)] if groups else []),
            *copy.deepcopy(WORKER_SUBAGENT_TOOLS),
        ]

    def permitted_names(self, stage: str) -> frozenset[str]:
        agenda = {str(spec["name"]) for spec in AGENDA_TOOL_SPECS}
        memory = {str(spec["name"]) for spec in MEMORY_TOOL_SPECS}
        thinking = {str(spec["name"]) for spec in THINKING_TOOL_SPECS}
        channel_tools = {spec["name"] for spec in self.planner_resident_specs()
                         if spec["name"] in {"qq_recall_message", "qq_call_status"}}
        delegation = {spec["name"] for spec in PLANNER_SUBAGENT_TOOLS}
        delegation.update(PLANNER_BUILTINS)
        shared = {"phone_result", "reply", "read_tool_result", *(spec["name"] for spec in IMAGE_TOOL_SPECS)}
        if self.affinity_enabled():
            shared.update(spec["name"] for spec in AFFINITY_TOOLS)
        general_chat = {
            "recall",
            "episode_relations",
            *shared,
            *agenda,
            *memory,
            *thinking,
            *channel_tools,
            *delegation,
        }
        if stage == "owner":
            return frozenset(general_chat | {"wait"})
        if stage == "heartbeat":
            return frozenset(general_chat | {"heartbeat_begin", "heartbeat_activity"})
        if stage in {"webhook", "subagent_report"}:
            return frozenset(general_chat)
        if stage == "goal":
            return frozenset(
                {
                    "goal_review",
                    "reply",
                    *channel_tools,
                    *delegation,
                    "goal_create",
                    "memory_search",
                    "read_tool_result",
                    *thinking,
                }
            )
        raise ValueError(f"stage does not use the conversation tool surface: {stage}")
