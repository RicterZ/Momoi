import copy
import json
import logging
from typing import Any, Callable

from ...tools.contracts.agenda import AGENDA_TOOL_SPECS
from ...tools.contracts.builtin import BUILTIN_TOOL_SPECS, builtin_tool_enabled
from ...observability.events import TRACE, log_event
from ...tools.contracts.memory import MEMORY_TOOL_SPECS
from ...tools.contracts.thinking import THINKING_TOOL_SPECS
from ...tools.contracts.images import IMAGE_TOOL_SPECS
from ...storage import estimate_tokens
from ..tool_contracts.context import RECALL_TOOL_SPEC, heartbeat_begin_spec
from ..tool_contracts.episode_relations import EPISODE_RELATIONS_TOOL_SPEC
from ..tool_contracts.current_state import current_state_finish_spec
from ..tool_contracts.conversation import (
    END_TURN_TOOL_SPEC, HEARTBEAT_ACTIVITY_TOOL_SPEC, GOAL_REVIEW_TOOL_SPEC,
)
from ..tool_contracts.runtime import (
    READ_TOOL_RESULT_SPEC,
    tool_search_spec,
    TOOL_ENABLE_SPEC,
)
from ..tool_contracts.plan import PLAN_TOOLS, PLAN_STEP_FINISH
from .progress import public_tool_spec
from ..tool_contracts.reply import REPLY_TOOL_SPEC
from ..tool_contracts.qq_call import QQ_CALL_STATUS_SPEC
from ..tool_contracts.qq_message import QQ_RECALL_MESSAGE_SPEC, QQ_POKE_SPEC

logger = logging.getLogger("momoi.runtime.turns")

DEFERRED_TOOLS = frozenset({
    "thinking_search", "thinking_read", "episode_relations", "episode_search", "memory_search",
    "write_file", "apply_patch", "makedirs", "move_file", "delete_file",
})
BUILTIN_GROUP_DESCRIPTIONS = {
    "builtin_history": "查询记忆、历史话题与关联话题，查看过去的思考记录。",
    "builtin_files": "写入、修改、移动、删除文件和创建目录。",
    "builtin_qq_messages": "QQ 私聊互动：戳一戳用户、撤回机器人已发送的消息。",
    "builtin_calls": "查询 QQ 语音电话的当前状态和基础服务是否就绪。",
}


class ToolSurface:
    """Projects the tool catalog exposed to each workflow."""

    def __init__(self, mcp: Any, channels: dict[str, Any], *, voice_enabled: bool = False, exec_enabled: bool = False, emotion_catalog: Callable[[], bool] | None = None, store: Any = None):
        self.mcp = mcp
        self.store = store
        self.channel_names = list(channels)
        self.voice_channels = [name for name, channel in channels.items()
                               if callable(getattr(channel, "send_voice", None))]
        self.voice_enabled = voice_enabled and bool(self.voice_channels)
        self.emotion_catalog = emotion_catalog or (lambda: True)
        self.builtin_specs = [
            spec for spec in BUILTIN_TOOL_SPECS
            if builtin_tool_enabled(spec["name"], exec_enabled=exec_enabled)
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

    def discovery_groups(self) -> dict[str, list[dict[str, Any]]]:
        groups = self.mcp_server_groups()
        groups["builtin_history"] = self.public_specs([
            spec for spec in [*MEMORY_TOOL_SPECS, *THINKING_TOOL_SPECS, EPISODE_RELATIONS_TOOL_SPEC]
            if spec["name"] in DEFERRED_TOOLS
        ])
        groups["builtin_files"] = self.public_specs([
            spec for spec in self.builtin_specs if spec["name"] in DEFERRED_TOOLS
        ])
        if "napcat" in self.channel_names:
            groups["builtin_qq_messages"] = self.public_specs([QQ_RECALL_MESSAGE_SPEC, QQ_POKE_SPEC])
        if "qq_call" in self.channel_names:
            groups["builtin_calls"] = self.public_specs([QQ_CALL_STATUS_SPEC])
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

    def tool_index(self) -> str:
        groups = self.discovery_groups()
        if not groups:
            return ""
        return "工具索引：\n" + "\n".join(
            f"- {group}: {self.mcp_group_description(group)}" for group in groups
        ) + "\n通过 tool_search 查找候选名称与描述，再用 tool_enable 批量加载选定工具。"

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
        groups = self.discovery_groups()
        reply_spec = copy.deepcopy(REPLY_TOOL_SPEC)
        reply_spec["input_schema"]["properties"]["channel"] = {
            "type": "string", "enum": self.channel_names,
            "description": "目标渠道；省略时回复当前消息所在渠道。",
        }
        if not self.voice_enabled:
            reply_spec["input_schema"]["properties"]["mode"]["enum"] = ["text"]
        tools = [
            copy.deepcopy(RECALL_TOOL_SPEC),
            heartbeat_begin_spec(),
            copy.deepcopy(HEARTBEAT_ACTIVITY_TOOL_SPEC),
            copy.deepcopy(GOAL_REVIEW_TOOL_SPEC),
            {**reply_spec, "description": REPLY_TOOL_SPEC["description"] + (" 已配置语音合成；语音渠道：" + "、".join(self.voice_channels) + "。在这些渠道可选 mode=voice，其他渠道使用 mode=text。" if self.voice_enabled else " 当前仅支持 mode=text，语音合成或渠道语音能力不可用。")},
            READ_TOOL_RESULT_SPEC,
            *copy.deepcopy([spec for spec in MEMORY_TOOL_SPECS if spec["name"] not in DEFERRED_TOOLS]),
            *copy.deepcopy(IMAGE_TOOL_SPECS),
            *self.public_specs(AGENDA_TOOL_SPECS),
            *self.public_specs(PLAN_TOOLS),
            copy.deepcopy(PLAN_STEP_FINISH),
            *self.public_specs([spec for spec in self.builtin_specs if spec["name"] not in DEFERRED_TOOLS]),
            *[
                spec for specs in groups.values() for spec in specs
                if spec.get("name") == "mcp__brave-search__brave_web_search"
            ],
            *([tool_search_spec(), copy.deepcopy(TOOL_ENABLE_SPEC)] if groups else []),
            current_state_finish_spec(),
            copy.deepcopy(END_TURN_TOOL_SPEC),
        ]
        catalog = {spec["name"]: spec for specs in groups.values() for spec in specs}
        if self.store is not None:
            self.append_visible(tools, [catalog[name] for name in self.store.transcript_enabled_tools() if name in catalog])
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
        shared = {"reply", "read_tool_result", *(spec["name"] for spec in IMAGE_TOOL_SPECS)}
        general_chat = {
            "recall",
            "episode_relations",
            "tool_search",
            "tool_enable",
            "end_turn",
            *shared,
            *agenda,
            *memory,
            *thinking,
            *external,
        }
        if stage == "owner":
            return frozenset(general_chat | {spec["name"] for spec in PLAN_TOOLS})
        if stage == "heartbeat":
            return frozenset(
                {
                    "heartbeat_begin",
                    "tool_search",
                    "tool_enable",
                    "recall",
                    "episode_relations",
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
            return frozenset({"reply", "web_fetch", "read_tool_result", "end_turn"})
        if stage == "reply_followup":
            return frozenset(general_chat)
        if stage == "goal":
            return frozenset(
                {
                    "goal_review",
                    "reply",
                    "goal_create",
                    "memory_search",
                    "read_tool_result",
                    "tool_search",
                    "tool_enable",
                    "end_turn",
                    *external,
                }
            )
        raise ValueError(f"stage does not use the conversation tool surface: {stage}")
