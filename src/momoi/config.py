import json
import os
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .channel import load_channel_config
from .policies import RuntimePolicies


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ThinkingConfig:
    effort: str = ""
    stages: dict[str, str] | None = None

    def for_stage(self, stage: str) -> str:
        return str((self.stages or {}).get(stage) or self.effort)


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
    tool_choice: bool = True
    thinking: ThinkingConfig = ThinkingConfig()


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
class DashboardConfig:
    token: str = ""


@dataclass(frozen=True)
class UsageConfig:
    provider: str = ""
    api_key: str = ""
    settings: dict[str, object] | None = None


@dataclass(frozen=True)
class ASRConfig:
    enabled: bool = False
    provider: str = "tencent"
    timeout_seconds: float = 30
    max_audio_bytes: int = 3 * 1024 * 1024
    settings: dict[str, object] | None = None


@dataclass(frozen=True)
class HeartbeatConfig:
    enabled: bool = False
    initial_delay_seconds: float = 900
    min_interval_seconds: float = 1800
    max_interval_seconds: float = 5400


@dataclass(frozen=True)
class AutonomyConfig:
    allowed_tools: tuple[str, ...] = ("curl", "read_file", "write_file", "list_dir")


@dataclass(frozen=True)
class ReflectionConfig:
    enabled: bool = False
    at: str = "03:00"


@dataclass(frozen=True)
class EpisodeAnnealingConfig:
    enabled: bool = True
    idle_seconds: float = 60
    max_seconds: float = 650


@dataclass(frozen=True)
class EmbeddingConfig:
    enabled: bool = False
    endpoint: str = "http://embedding:8002/v1/embeddings"
    api_key: str = ""
    model: str = "BAAI/bge-small-zh-v1.5"
    dimensions: int = 512
    calibration_profile: str = "bge-small-zh-v1.5-momoi-v1"
    query_timeout_seconds: float = 5
    document_timeout_seconds: float = 30
    document_batch_size: int = 8


@dataclass(frozen=True)
class AppConfig:
    llm: LLMConfig
    channel: object
    system_prompt: str
    transcript_turns_min: int
    transcript_turns_max: int
    episode_raw_tail_turns: int
    memory_results: int
    memory_tokens: int
    database: Path
    log_level: str
    max_input_tokens: int = 142222
    context_compaction_ratio: float = 0.9
    summary_results: int = 8
    summary_tokens: int = 6000
    soul_prompt: str = ""
    mcp_config: Path | None = None
    notifications: NotificationConfig = NotificationConfig()
    tool_result_max_chars: int = 12000
    tool_result_retention_days: float = 30
    turn_max_seconds: float = 0
    turn_max_total_tokens: int = 0
    webhooks: WebhookConfig = WebhookConfig()
    dashboard: DashboardConfig = DashboardConfig()
    usage: UsageConfig = UsageConfig()
    heartbeat: HeartbeatConfig = HeartbeatConfig()
    autonomy: AutonomyConfig = AutonomyConfig()
    reflection: ReflectionConfig = ReflectionConfig()
    episode_annealing: EpisodeAnnealingConfig = EpisodeAnnealingConfig()
    workspace: Path | None = None
    heartbeat_prompt: str = ""
    soul_prompt_path: Path | None = None
    heartbeat_prompt_path: Path | None = None
    thinking: Path | None = None
    channels: tuple[object, ...] = ()
    policies: RuntimePolicies = RuntimePolicies()
    asr: ASRConfig = ASRConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()

    @property
    def channel_configs(self) -> tuple[object, ...]:
        return self.channels or (self.channel,)

    @property
    def context_compaction_tokens(self) -> int:
        return max(1, round(self.max_input_tokens * self.context_compaction_ratio))


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


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _env_bool(name: str) -> bool | None:
    value = _env(name).lower()
    if not value:
        return None
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _apply_env_overrides(raw: dict[str, Any]) -> None:
    llm = raw.setdefault("llm", {})
    if isinstance(llm, dict):
        if value := _env("MOMOI_LLM_API_FORMAT"):
            llm["api_format"] = value
        if value := _env("MOMOI_LLM_BASE_URL"):
            llm["base_url"] = value
        if value := _env("MOMOI_LLM_API_KEY"):
            llm["api_key"] = value
        if value := _env("MOMOI_LLM_MODEL"):
            llm["model"] = value

    channels = raw.get("channels")
    if isinstance(channels, dict):
        if value := _env("MOMOI_PRIMARY"):
            channels["primary"] = value
        enabled = channels.get("enabled")
        napcat = enabled.get("napcat") if isinstance(enabled, dict) else None
        if isinstance(napcat, dict):
            if value := _env("MOMOI_NAPCAT_URL"):
                napcat["url"] = value
            if value := _env("MOMOI_OWNER_QQ"):
                napcat["owner_qq"] = value

    notifications = raw.setdefault("notifications", {})
    if isinstance(notifications, dict) and (value := _env("MOMOI_TIMEZONE")):
        notifications["timezone"] = value

    dashboard = raw.setdefault("dashboard", {})
    if isinstance(dashboard, dict) and (value := _env("MOMOI_DASHBOARD_TOKEN")):
        dashboard["token"] = value

    webhooks = raw.setdefault("webhooks", {})
    if isinstance(webhooks, dict):
        enabled = _env_bool("MOMOI_WEBHOOKS_ENABLED")
        if enabled is not None:
            webhooks["enabled"] = enabled
        if value := _env("MOMOI_WEBHOOKS_HOST"):
            webhooks["host"] = value
        if value := _env("MOMOI_WEBHOOKS_TOKEN"):
            webhooks["token"] = value

    usage = raw.setdefault("usage", {})
    if isinstance(usage, dict) and (value := _env("MOMOI_USAGE_API_KEY")):
        usage["api_key"] = value

    asr = raw.setdefault("asr", {})
    if isinstance(asr, dict):
        settings = asr.setdefault("settings", {})
        if isinstance(settings, dict):
            if value := _env("MOMOI_ASR_SECRET_ID"):
                settings["secret_id"] = value
            if value := _env("MOMOI_ASR_SECRET_KEY"):
                settings["secret_key"] = value


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as file:
        raw = json.load(file)
    if not isinstance(raw, dict):
        raise ConfigError("config.json must be a table/object")
    _apply_env_overrides(raw)

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
    tool_choice = _boolean(llm_raw.get("tool_choice", True), "llm.tool_choice")
    thinking_raw = _mapping(llm_raw.get("thinking") or {}, "llm.thinking")
    thinking_effort = str(thinking_raw.get("effort") or "").lower()
    allowed_thinking_efforts = {"", "low", "high", "max"}
    if thinking_effort not in allowed_thinking_efforts:
        raise ConfigError("llm.thinking.effort must be low, high, or max")
    raw_thinking_stages = _mapping(
        thinking_raw.get("stages", {}),
        "llm.thinking.stages",
    )
    thinking_stages = {
        str(stage).strip(): str(effort).lower()
        for stage, effort in raw_thinking_stages.items()
    }
    if any(not stage for stage in thinking_stages):
        raise ConfigError("llm.thinking.stages keys must not be empty")
    if any(
        effort not in allowed_thinking_efforts - {""}
        for effort in thinking_stages.values()
    ):
        raise ConfigError(
            "llm.thinking.stages values must be low, high, or max"
        )

    try:
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
    heartbeat_path = (
        config_path.parent
        / str(context_raw.get("heartbeat_prompt", "prompts/HEARTBEAT.md"))
    ).resolve()
    system_prompt = files("momoi").joinpath("prompts/system.md").read_text(encoding="utf-8").strip()
    soul_prompt = soul_path.read_text(encoding="utf-8").strip()
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
    thinking_value = storage_raw.get("thinking")
    if thinking_value in (None, ""):
        thinking_dir = database.parent
    else:
        thinking_dir = Path(str(thinking_value)).expanduser()
        if not thinking_dir.is_absolute():
            thinking_dir = (config_path.parent / thinking_dir).resolve()
    thinking_dir.mkdir(parents=True, exist_ok=True)
    tools_raw = _mapping(raw.get("tools", {}), "tools")
    turn_raw = _mapping(raw.get("turn", {}), "turn")
    webhook_raw = _mapping(raw.get("webhooks", {}), "webhooks")
    dashboard_raw = _mapping(raw.get("dashboard", {}), "dashboard")
    usage_raw = _mapping(raw.get("usage", {}), "usage")
    usage_settings = {
        key: value
        for key, value in usage_raw.items()
        if key not in {"provider", "api_key"}
    }
    asr_raw = _mapping(raw.get("asr", {}), "asr")
    asr_enabled = _boolean(asr_raw.get("enabled", False), "asr.enabled")
    asr_provider = str(asr_raw.get("provider") or "tencent").strip()
    if not asr_provider:
        raise ConfigError("asr.provider must not be empty")
    asr_settings = _mapping(asr_raw.get("settings", {}), "asr.settings")
    asr_timeout = _positive(
        asr_raw.get("timeout_seconds", 30), "asr.timeout_seconds"
    )
    asr_max_audio_bytes = int(asr_raw.get("max_audio_bytes", 3 * 1024 * 1024))
    if asr_max_audio_bytes <= 0:
        raise ConfigError("asr.max_audio_bytes must be positive")
    if asr_enabled and asr_provider == "tencent":
        if not str(asr_settings.get("secret_id") or "").strip():
            raise ConfigError(
                "asr.settings.secret_id is required when Tencent ASR is enabled"
            )
        if not str(asr_settings.get("secret_key") or "").strip():
            raise ConfigError(
                "asr.settings.secret_key is required when Tencent ASR is enabled"
            )
    heartbeat_raw = _mapping(raw.get("heartbeat", {}), "heartbeat")
    autonomy_raw = _mapping(raw.get("autonomy", {}), "autonomy")
    reflection_raw = _mapping(raw.get("reflection", {}), "reflection")
    annealing_raw = _mapping(
        raw.get("episode_annealing", {}), "episode_annealing"
    )
    embedding_raw = _mapping(raw.get("embedding", {}), "embedding")
    embedding_enabled = _boolean(
        embedding_raw.get("enabled", False), "embedding.enabled"
    )
    embedding_model = str(
        embedding_raw.get("model", "BAAI/bge-small-zh-v1.5")
    ).strip()
    embedding_dimensions = int(embedding_raw.get("dimensions", 512))
    embedding_profile = str(
        embedding_raw.get(
            "calibration_profile", "bge-small-zh-v1.5-momoi-v1"
        )
    ).strip()
    if embedding_enabled and not embedding_model:
        raise ConfigError("embedding.model is required when embedding is enabled")
    if embedding_dimensions <= 0:
        raise ConfigError("embedding.dimensions must be positive")
    if not embedding_profile:
        raise ConfigError("embedding.calibration_profile must not be empty")
    document_batch_size = int(embedding_raw.get("document_batch_size", 8))
    if document_batch_size <= 0:
        raise ConfigError("embedding.document_batch_size must be positive")
    dashboard_token = str(dashboard_raw.get("token") or "")
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
        / str(webhook_raw.get("executors", "workflows/workflow-executors.yaml"))
    ).resolve()
    heartbeat_min = _positive(
        heartbeat_raw.get("min_interval_seconds", 1800),
        "heartbeat.min_interval_seconds",
    )
    heartbeat_max = _positive(
        heartbeat_raw.get("max_interval_seconds", 5400),
        "heartbeat.max_interval_seconds",
    )
    if heartbeat_max < heartbeat_min:
        raise ConfigError(
            "heartbeat.max_interval_seconds must be at least min_interval_seconds"
        )
    allowed_tools = autonomy_raw.get(
        "allowed_tools", ["curl", "read_file", "write_file", "list_dir"]
    )
    if not isinstance(allowed_tools, list) or not all(
        isinstance(item, str) and item.strip() for item in allowed_tools
    ):
        raise ConfigError("autonomy.allowed_tools must be an array of tool names")
    max_input_tokens = max(
        1000, int(context_raw.get("max_input_tokens", 142222))
    )
    context_compaction_ratio = float(
        context_raw.get("context_compaction_ratio", 0.9)
    )
    if not 0 < context_compaction_ratio <= 1:
        raise ConfigError("context.context_compaction_ratio must be between 0 and 1")
    transcript_turns_min = max(1, int(context_raw.get("transcript_turns_min", 48)))
    transcript_turns_max = max(
        transcript_turns_min,
        int(context_raw.get("transcript_turns_max", 96)),
    )
    episode_raw_tail_turns = max(
        1, int(context_raw.get("episode_raw_tail_turns", 6))
    )

    return AppConfig(
        llm=LLMConfig(
            base_url=str(llm_raw["base_url"]).rstrip("/"),
            api_key=api_key,
            model=model,
            max_tokens=int(llm_raw.get("max_tokens", 16384)),
            temperature=float(llm_raw.get("temperature", 0.6)),
            timeout_seconds=_positive(llm_raw.get("timeout_seconds", 300), "llm.timeout_seconds"),
            max_retries=max(0, int(llm_raw.get("max_retries", 3))),
            api_format=api_format,
            tool_choice=tool_choice,
            thinking=ThinkingConfig(
                effort=thinking_effort,
                stages=thinking_stages,
            ),
        ),
        channel=channel_config,
        system_prompt=system_prompt,
        transcript_turns_min=transcript_turns_min,
        transcript_turns_max=transcript_turns_max,
        episode_raw_tail_turns=episode_raw_tail_turns,
        memory_results=min(6, max(0, int(context_raw.get("memory_results", 6)))),
        memory_tokens=max(0, int(context_raw.get("memory_tokens", 8000))),
        database=database,
        log_level=str(logging_raw.get("level", "DEBUG")).upper(),
        max_input_tokens=max_input_tokens,
        context_compaction_ratio=context_compaction_ratio,
        summary_results=min(
            12, max(0, int(context_raw.get("summary_results", 8)))
        ),
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
            1000, int(tools_raw.get("result_max_chars", 12000))
        ),
        tool_result_retention_days=_nonnegative(
            tools_raw.get("result_retention_days", 30),
            "tools.result_retention_days",
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
        dashboard=DashboardConfig(token=dashboard_token),
        usage=UsageConfig(
            provider=str(usage_raw.get("provider") or ""),
            api_key=str(usage_raw.get("api_key") or ""),
            settings=usage_settings or None,
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
        episode_annealing=EpisodeAnnealingConfig(
            enabled=_boolean(
                annealing_raw.get("enabled", True),
                "episode_annealing.enabled",
            ),
            idle_seconds=_nonnegative(
                annealing_raw.get("idle_seconds", 60),
                "episode_annealing.idle_seconds",
            ),
            max_seconds=_positive(
                annealing_raw.get("max_seconds", 650),
                "episode_annealing.max_seconds",
            ),
        ),
        workspace=config_path.parent,
        soul_prompt_path=soul_path,
        heartbeat_prompt_path=heartbeat_path,
        thinking=thinking_dir,
        channels=channel_configs,
        asr=ASRConfig(
            enabled=asr_enabled,
            provider=asr_provider,
            timeout_seconds=asr_timeout,
            max_audio_bytes=asr_max_audio_bytes,
            settings=dict(asr_settings) or None,
        ),
        embedding=EmbeddingConfig(
            enabled=embedding_enabled,
            endpoint=str(
                embedding_raw.get(
                    "endpoint", "http://embedding:8002/v1/embeddings"
                )
            ).rstrip("/"),
            api_key=str(embedding_raw.get("api_key") or ""),
            model=embedding_model,
            dimensions=embedding_dimensions,
            calibration_profile=embedding_profile,
            query_timeout_seconds=_positive(
                embedding_raw.get("query_timeout_seconds", 5),
                "embedding.query_timeout_seconds",
            ),
            document_timeout_seconds=_positive(
                embedding_raw.get("document_timeout_seconds", 30),
                "embedding.document_timeout_seconds",
            ),
            document_batch_size=document_batch_size,
        ),
    )
