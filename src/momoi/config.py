import json
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .channel import load_channel_config


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    max_tokens: int
    temperature: float
    timeout_seconds: float
    max_retries: int
    api_format: str = "anthropic"


@dataclass(frozen=True)
class NotificationConfig:
    timezone: str = "UTC"
    quiet_start: str | None = None
    quiet_end: str | None = None
    cooldown_seconds: float = 1800
    pending_owner_delay_seconds: float = 30


@dataclass(frozen=True)
class WebhookConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8787
    token: str = ""
    workflows: Path | None = None
    executors: Path | None = None


@dataclass(frozen=True)
class HeartbeatConfig:
    enabled: bool = False
    initial_delay_seconds: float = 900
    min_interval_seconds: float = 1800
    max_interval_seconds: float = 5400
    reply_initial_interval_seconds: float = 60


@dataclass(frozen=True)
class AutonomyConfig:
    allowed_tools: tuple[str, ...] = ("curl", "read_file", "write_file")


@dataclass(frozen=True)
class ReflectionConfig:
    enabled: bool = False
    at: str = "03:00"


@dataclass(frozen=True)
class AppConfig:
    llm: LLMConfig
    channel: object
    system_prompt: str
    recent_raw_tokens: int
    recent_turns: int
    memory_results: int
    memory_tokens: int
    database: Path
    log_level: str
    max_input_tokens: int = 96000
    summary_results: int = 3
    summary_tokens: int = 6000
    soul_prompt: str = ""
    mcp_config: Path | None = None
    notifications: NotificationConfig = NotificationConfig()
    tool_result_max_chars: int = 30000
    turn_max_seconds: float = 0
    turn_max_total_tokens: int = 0
    webhooks: WebhookConfig = WebhookConfig()
    heartbeat: HeartbeatConfig = HeartbeatConfig()
    autonomy: AutonomyConfig = AutonomyConfig()
    reflection: ReflectionConfig = ReflectionConfig()
    workspace: Path | None = None
    heartbeat_prompt: str = ""
    soul_prompt_path: Path | None = None
    heartbeat_prompt_path: Path | None = None
    channels: tuple[object, ...] = ()

    @property
    def channel_configs(self) -> tuple[object, ...]:
        return self.channels or (self.channel,)

def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a table/object")
    return value


def _positive(value: Any, name: str) -> float:
    number = float(value)
    if number <= 0:
        raise ConfigError(f"{name} must be positive")
    return number


def _nonnegative(value: Any, name: str) -> float:
    number = float(value)
    if number < 0:
        raise ConfigError(f"{name} must not be negative")
    return number


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be boolean")
    return value


def _clock(value: Any, name: str) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise ConfigError(f"{name} must use HH:MM")
    return text


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as file:
        raw = json.load(file)

    llm_raw = _mapping(raw.get("llm"), "llm")
    model = str(llm_raw.get("model") or "")
    api_key = str(llm_raw.get("api_key") or "")
    if not api_key:
        raise ConfigError("llm.api_key is required")
    if not model:
        raise ConfigError("llm.model is required")
    api_format = str(llm_raw.get("api_format", "anthropic")).lower()
    if api_format not in {"anthropic", "openai"}:
        raise ConfigError("llm.api_format must be anthropic or openai")

    if "channel" in raw and "channels" in raw:
        raise ConfigError("configure either channel or channels, not both")
    try:
        if "channels" in raw:
            channel_section = _mapping(raw.get("channels"), "channels")
            primary_name = str(channel_section.get("primary") or "")
            enabled = _mapping(channel_section.get("enabled"), "channels.enabled")
            if not enabled:
                raise ConfigError("channels.enabled must not be empty")
            if primary_name not in enabled:
                raise ConfigError("channels.primary must name an enabled channel")
            channel_configs = tuple(
                load_channel_config(
                    str(name),
                    _mapping(settings, f"channels.enabled.{name}"),
                    config_path.parent,
                )
                for name, settings in enabled.items()
            )
            channel_config = next(
                item
                for item in channel_configs
                if getattr(item, "plugin", "") == primary_name
            )
        else:
            channel_section = _mapping(raw.get("channel"), "channel")
            channel_name = str(channel_section.get("plugin") or "")
            if not channel_name:
                raise ConfigError("channel.plugin is required")
            channel_settings = _mapping(
                channel_section.get("settings"), "channel.settings"
            )
            channel_config = load_channel_config(
                channel_name, channel_settings, config_path.parent
            )
            channel_configs = (channel_config,)
    except (TypeError, ValueError) as error:
        raise ConfigError(str(error)) from None

    context_raw = _mapping(raw.get("context"), "context")
    storage_raw = _mapping(raw.get("storage"), "storage")
    logging_raw = _mapping(raw.get("logging"), "logging")
    notification_raw = _mapping(raw.get("notifications", {}), "notifications")
    notification_timezone = str(notification_raw.get("timezone", "UTC"))
    try:
        ZoneInfo(notification_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("notifications.timezone must be a valid IANA timezone") from None
    quiet_start = _clock(notification_raw.get("quiet_start"), "notifications.quiet_start")
    quiet_end = _clock(notification_raw.get("quiet_end"), "notifications.quiet_end")
    if (quiet_start is None) != (quiet_end is None) or (
        quiet_start is not None and quiet_start == quiet_end
    ):
        raise ConfigError(
            "notifications quiet_start and quiet_end must be distinct or both omitted"
        )
    soul_path = (
        config_path.parent / str(context_raw.get("soul_prompt", "prompts/SOUL.md"))
    ).resolve()
    system_prompt = files("momoi").joinpath("prompts/system.md").read_text(encoding="utf-8").strip()
    soul_prompt = soul_path.read_text(encoding="utf-8").strip()
    heartbeat_path = config_path.parent / "HEARTBEAT.md"
    heartbeat_prompt = (
        heartbeat_path.read_text(encoding="utf-8").strip()
        if heartbeat_path.is_file()
        else ""
    )
    if not system_prompt or not soul_prompt:
        raise ConfigError("system and soul prompts must not be empty")

    database = Path(str(storage_raw["database"])).expanduser()
    if not database.is_absolute():
        database = (config_path.parent / database).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    tools_raw = _mapping(raw.get("tools", {}), "tools")
    turn_raw = _mapping(raw.get("turn", {}), "turn")
    webhook_raw = _mapping(raw.get("webhooks", {}), "webhooks")
    heartbeat_raw = _mapping(raw.get("heartbeat", {}), "heartbeat")
    autonomy_raw = _mapping(raw.get("autonomy", {}), "autonomy")
    reflection_raw = _mapping(raw.get("reflection", {}), "reflection")
    mcp_value = tools_raw.get("mcp_config", "mcp.json")
    mcp_config = (config_path.parent / str(mcp_value)).resolve() if mcp_value else None
    webhook_enabled = _boolean(webhook_raw.get("enabled", False), "webhooks.enabled")
    webhook_token = str(webhook_raw.get("token") or "")
    if webhook_enabled and not webhook_token:
        raise ConfigError("webhooks.token is required when webhooks are enabled")
    webhook_port = int(webhook_raw.get("port", 8787))
    if not 1 <= webhook_port <= 65535:
        raise ConfigError("webhooks.port must be between 1 and 65535")
    workflow_path = (
        config_path.parent / str(webhook_raw.get("workflows", "workflows"))
    ).resolve()
    executor_path = (
        config_path.parent
        / str(webhook_raw.get("executors", "workflow-executors.yaml"))
    ).resolve()
    heartbeat_min = _positive(
        heartbeat_raw.get("min_interval_seconds", 1800),
        "heartbeat.min_interval_seconds",
    )
    heartbeat_max = _positive(
        heartbeat_raw.get("max_interval_seconds", 5400),
        "heartbeat.max_interval_seconds",
    )
    heartbeat_reply_initial = _positive(
        heartbeat_raw.get("reply_initial_interval_seconds", 60),
        "heartbeat.reply_initial_interval_seconds",
    )
    if heartbeat_max < heartbeat_min:
        raise ConfigError(
            "heartbeat.max_interval_seconds must be at least min_interval_seconds"
        )
    allowed_tools = autonomy_raw.get(
        "allowed_tools", ["curl", "read_file", "write_file"]
    )
    if not isinstance(allowed_tools, list) or not all(
        isinstance(item, str) and item.strip() for item in allowed_tools
    ):
        raise ConfigError("autonomy.allowed_tools must be an array of tool names")

    return AppConfig(
        llm=LLMConfig(
            base_url=str(llm_raw["base_url"]).rstrip("/"),
            api_key=api_key,
            model=model,
            max_tokens=int(llm_raw.get("max_tokens", 2048)),
            temperature=float(llm_raw.get("temperature", 0.6)),
            timeout_seconds=_positive(llm_raw.get("timeout_seconds", 120), "llm.timeout_seconds"),
            max_retries=max(0, int(llm_raw.get("max_retries", 2))),
            api_format=api_format,
        ),
        channel=channel_config,
        system_prompt=system_prompt,
        recent_raw_tokens=max(1, int(context_raw.get("recent_raw_tokens", 32000))),
        recent_turns=max(1, int(context_raw.get("recent_turns", 6))),
        memory_results=max(0, int(context_raw.get("memory_results", 6))),
        memory_tokens=max(0, int(context_raw.get("memory_tokens", 8000))),
        database=database,
        log_level=str(logging_raw.get("level", "DEBUG")).upper(),
        max_input_tokens=max(1000, int(context_raw.get("max_input_tokens", 96000))),
        summary_results=max(0, int(context_raw.get("summary_results", 3))),
        summary_tokens=max(0, int(context_raw.get("summary_tokens", 6000))),
        soul_prompt=soul_prompt,
        heartbeat_prompt=heartbeat_prompt,
        mcp_config=mcp_config,
        notifications=NotificationConfig(
            timezone=notification_timezone,
            quiet_start=quiet_start,
            quiet_end=quiet_end,
            cooldown_seconds=_nonnegative(
                notification_raw.get("cooldown_seconds", 1800),
                "notifications.cooldown_seconds",
            ),
            pending_owner_delay_seconds=_nonnegative(
                notification_raw.get("pending_owner_delay_seconds", 30),
                "notifications.pending_owner_delay_seconds",
            ),
        ),
        tool_result_max_chars=max(
            1000, int(tools_raw.get("result_max_chars", 30000))
        ),
        turn_max_seconds=_nonnegative(
            turn_raw.get("max_seconds", 0), "turn.max_seconds"
        ),
        turn_max_total_tokens=max(
            0, int(turn_raw.get("max_total_tokens", 0))
        ),
        webhooks=WebhookConfig(
            enabled=webhook_enabled,
            host=str(webhook_raw.get("host", "127.0.0.1")),
            port=webhook_port,
            token=webhook_token,
            workflows=workflow_path,
            executors=executor_path,
        ),
        heartbeat=HeartbeatConfig(
            enabled=_boolean(
                heartbeat_raw.get("enabled", False), "heartbeat.enabled"
            ),
            initial_delay_seconds=_positive(
                heartbeat_raw.get("initial_delay_seconds", 900),
                "heartbeat.initial_delay_seconds",
            ),
            min_interval_seconds=heartbeat_min,
            max_interval_seconds=heartbeat_max,
            reply_initial_interval_seconds=heartbeat_reply_initial,
        ),
        autonomy=AutonomyConfig(
            tuple(dict.fromkeys(item.strip() for item in allowed_tools))
        ),
        reflection=ReflectionConfig(
            enabled=_boolean(
                reflection_raw.get("enabled", False), "reflection.enabled"
            ),
            at=_clock(reflection_raw.get("at", "03:00"), "reflection.at")
            or "03:00",
        ),
        workspace=config_path.parent,
        soul_prompt_path=soul_path,
        heartbeat_prompt_path=heartbeat_path,
        channels=channel_configs,
    )
