"""Dashboard field descriptions for editable application controls."""

import copy

LOG_LEVELS = ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_FIELDS = {
    "heartbeat": {
        "label": "心跳",
        "fields": {
            "enabled": {"type": "boolean", "label": "启用心跳", "default": False},
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
