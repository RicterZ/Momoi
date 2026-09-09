"""Dashboard field descriptions for editable application controls."""

import copy

from ..integrations.request_context import THINKING_EFFORTS
LOG_LEVELS = ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
THINKING_STAGES = {
    "owner": "用户对话",
    "topic_selection": "话题召回筛选",
    "heartbeat": "心跳",
    "reply_followup": "回复跟进",
    "webhook": "Webhook",
    "goal": "目标执行",
    "reflection": "每日复盘",
    "memory_maintenance": "记忆维护",
    "memory_operation": "记忆操作",
    "episode_consolidate": "话题整理",
    "episode_anneal": "话题归档",
    "current_state_maintenance": "当前状态维护",
}

_FIELDS = {
    "tools": {
        "label": "工具",
        "fields": {
            "exec_enabled": {"type": "boolean", "label": "命令执行（exec）", "default": False},
        },
    },
    "thinking": {
        "label": "阶段思考强度",
        "fields": {
            "stages": {
                "type": "object",
                "label": "运行阶段",
                "default": {},
                "description": "覆盖各运行阶段的思考强度；留空跟随当前模型设置。切换模型不会清空。",
                "properties": {
                    stage: {
                        "type": "string",
                        "label": label,
                        "enum": ["", *THINKING_EFFORTS],
                        "default": "low" if stage == "topic_selection" else "",
                        "advanced": False,
                        "description": "空字符串表示跟随模型；low / medium / high / xhigh / max 表示该阶段的思考强度，原样交给服务端处理。",
                    }
                    for stage, label in THINKING_STAGES.items()
                },
            },
        },
    },
    "heartbeat": {
        "label": "心跳",
        "fields": {
            "enabled": {"type": "boolean", "label": "启用心跳", "default": True},
        },
    },
    "logging": {
        "label": "日志",
        "fields": {
            "level": {
                "type": "string",
                "label": "日志级别",
                "default": "DEBUG",
                "enum": list(LOG_LEVELS),
            },
        },
    },
    "reflection": {
        "label": "每日复盘",
        "fields": {
            "enabled": {"type": "boolean", "label": "启用复盘", "default": False},
            "at": {
                "type": "string",
                "label": "复盘时间",
                "default": "03:00",
                "format": "time",
                "pattern": r"^(?:[01]\d|2[0-3]):[0-5]\d$",
                "description": "每天执行复盘的时间（HH:MM），使用应用配置的时区。",
            },
        },
    },
    "episode_annealing": {
        "label": "Episode 退火",
        "fields": {
            "enabled": {"type": "boolean", "label": "启用 Episode 退火", "default": True},
        },
    },
}


def runtime_fields() -> dict:
    result = copy.deepcopy(_FIELDS)
    for section in result.values():
        for spec in section["fields"].values():
            spec["advanced"] = False
    return result
