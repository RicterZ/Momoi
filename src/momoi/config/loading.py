import json
from importlib.resources import files
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..channel import load_channel_config
from .environment import apply_env_overrides
from .models import (
    AppConfig,
    ConfigError,
    DashboardConfig,
    EpisodeAnnealingConfig,
    HeartbeatConfig,
    NotificationConfig,
    ReflectionConfig,
    WebhookConfig,
)
from .validation import boolean, clock, integer, mapping, nonnegative, positive


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as file:
        raw = json.load(file)
    if not isinstance(raw, dict):
        raise ConfigError("config.json must be a table/object")
    allowed = {
        "providers",
        "timezone",
        "channels",
        "context",
        "storage",
        "logging",
        "notifications",
        "tools",
        "turn",
        "webhooks",
        "dashboard",
        "heartbeat",
        "reflection",
        "episode_annealing",
    }
    if unknown := set(raw) - allowed:
        raise ConfigError(f"unknown configuration field: {sorted(unknown)[0]}")
    from ..integrations.configuration import resolve_provider_config

    providers = resolve_provider_config(raw, config_path)
    apply_env_overrides(raw)

    try:
        channel_section = mapping(raw.get("channels"), "channels")
        primary_name = str(channel_section.get("primary") or "")
        enabled = mapping(channel_section.get("enabled"), "channels.enabled")
        if not enabled:
            raise ConfigError("channels.enabled must not be empty")
        if primary_name not in enabled:
            raise ConfigError("channels.primary must name an enabled channel")
        channel_configs = tuple(
            load_channel_config(
                str(name),
                mapping(settings, f"channels.enabled.{name}"),
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

    context_raw = mapping(raw.get("context"), "context")
    storage_raw = mapping(raw.get("storage"), "storage")
    logging_raw = mapping(raw.get("logging"), "logging")
    notification_raw = mapping(raw.get("notifications", {}), "notifications")
    if "timezone" in notification_raw:
        raise ConfigError(
            "notifications.timezone was removed; use the top-level timezone"
        )
    app_timezone = str(raw.get("timezone", "UTC"))
    try:
        ZoneInfo(app_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("timezone must be a valid IANA timezone") from None
    quiet_start = clock(
        notification_raw.get("quiet_start"), "notifications.quiet_start"
    )
    quiet_end = clock(notification_raw.get("quiet_end"), "notifications.quiet_end")
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
    system_prompt = (
        files("momoi").joinpath("prompts/system.md").read_text(encoding="utf-8").strip()
    )
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
    tools_raw = mapping(raw.get("tools", {}), "tools")
    turn_raw = mapping(raw.get("turn", {}), "turn")
    webhook_raw = mapping(raw.get("webhooks", {}), "webhooks")
    dashboard_raw = mapping(raw.get("dashboard", {}), "dashboard")
    heartbeat_raw = mapping(raw.get("heartbeat", {}), "heartbeat")
    reflection_raw = mapping(raw.get("reflection", {}), "reflection")
    annealing_raw = mapping(raw.get("episode_annealing", {}), "episode_annealing")
    dashboard_token = str(dashboard_raw.get("token") or "")
    mcp_value = tools_raw.get("mcp_config", "mcp.json")
    mcp_config = (config_path.parent / str(mcp_value)).resolve() if mcp_value else None
    webhook_enabled = boolean(webhook_raw.get("enabled", False), "webhooks.enabled")
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
    heartbeat_min = positive(
        heartbeat_raw.get("min_interval_seconds", 1800),
        "heartbeat.min_interval_seconds",
    )
    heartbeat_max = positive(
        heartbeat_raw.get("max_interval_seconds", 5400),
        "heartbeat.max_interval_seconds",
    )
    if heartbeat_max < heartbeat_min:
        raise ConfigError(
            "heartbeat.max_interval_seconds must be at least min_interval_seconds"
        )
    max_input_tokens = integer(
        context_raw.get("max_input_tokens", 142222),
        "context.max_input_tokens",
        minimum=1000,
    )
    context_compaction_ratio = float(context_raw.get("context_compaction_ratio", 0.9))
    if not 0 < context_compaction_ratio <= 1:
        raise ConfigError("context.context_compaction_ratio must be between 0 and 1")
    transcript_turns_min = integer(
        context_raw.get("transcript_turns_min", 32),
        "context.transcript_turns_min",
        minimum=1,
    )
    transcript_turns_max = integer(
        context_raw.get("transcript_turns_max", 80),
        "context.transcript_turns_max",
        minimum=transcript_turns_min,
    )
    episode_raw_tail_turns = integer(
        context_raw.get("episode_raw_tail_turns", 6),
        "context.episode_raw_tail_turns",
        minimum=1,
    )
    memory_results = integer(
        context_raw.get("memory_results", 6),
        "context.memory_results",
        minimum=0,
        maximum=6,
    )
    summary_results = integer(
        context_raw.get("summary_results", 8),
        "context.summary_results",
        minimum=0,
        maximum=12,
    )
    summary_tokens = integer(
        context_raw.get("summary_tokens", 6000),
        "context.summary_tokens",
        minimum=0,
    )
    tool_result_max_chars = integer(
        tools_raw.get("result_max_chars", 12000),
        "tools.result_max_chars",
        minimum=1000,
    )
    turn_max_total_tokens = integer(
        turn_raw.get("max_total_tokens", 0),
        "turn.max_total_tokens",
        minimum=0,
    )

    return AppConfig(
        channel=channel_config,
        system_prompt=system_prompt,
        transcript_turns_min=transcript_turns_min,
        transcript_turns_max=transcript_turns_max,
        episode_raw_tail_turns=episode_raw_tail_turns,
        memory_results=memory_results,
        database=database,
        log_level=str(logging_raw.get("level", "DEBUG")).upper(),
        timezone=app_timezone,
        max_input_tokens=max_input_tokens,
        context_compaction_ratio=context_compaction_ratio,
        summary_results=summary_results,
        summary_tokens=summary_tokens,
        soul_prompt=soul_prompt,
        heartbeat_prompt=heartbeat_prompt,
        mcp_config=mcp_config,
        notifications=NotificationConfig(
            quiet_start=quiet_start,
            quiet_end=quiet_end,
            cooldown_seconds=nonnegative(
                notification_raw.get("cooldown_seconds", 1800),
                "notifications.cooldown_seconds",
            ),
            pending_owner_delay_seconds=nonnegative(
                notification_raw.get("pending_owner_delay_seconds", 30),
                "notifications.pending_owner_delay_seconds",
            ),
        ),
        tool_result_max_chars=tool_result_max_chars,
        tool_result_retention_days=nonnegative(
            tools_raw.get("result_retention_days", 30),
            "tools.result_retention_days",
        ),
        turn_max_seconds=nonnegative(
            turn_raw.get("max_seconds", 0), "turn.max_seconds"
        ),
        turn_max_total_tokens=turn_max_total_tokens,
        webhooks=WebhookConfig(
            enabled=webhook_enabled,
            host=str(webhook_raw.get("host", "127.0.0.1")),
            port=webhook_port,
            token=webhook_token,
            workflows=workflow_path,
            executors=executor_path,
        ),
        dashboard=DashboardConfig(token=dashboard_token),
        heartbeat=HeartbeatConfig(
            enabled=boolean(heartbeat_raw.get("enabled", False), "heartbeat.enabled"),
            initial_delay_seconds=positive(
                heartbeat_raw.get("initial_delay_seconds", 900),
                "heartbeat.initial_delay_seconds",
            ),
            min_interval_seconds=heartbeat_min,
            max_interval_seconds=heartbeat_max,
        ),
        reflection=ReflectionConfig(
            enabled=boolean(reflection_raw.get("enabled", False), "reflection.enabled"),
            at=clock(reflection_raw.get("at", "03:00"), "reflection.at") or "03:00",
        ),
        episode_annealing=EpisodeAnnealingConfig(
            enabled=boolean(
                annealing_raw.get("enabled", True),
                "episode_annealing.enabled",
            ),
            idle_seconds=nonnegative(
                annealing_raw.get("idle_seconds", 60),
                "episode_annealing.idle_seconds",
            ),
            max_seconds=positive(
                annealing_raw.get("max_seconds", 650),
                "episode_annealing.max_seconds",
            ),
        ),
        workspace=config_path.parent,
        soul_prompt_path=soul_path,
        heartbeat_prompt_path=heartbeat_path,
        thinking=thinking_dir,
        channels=channel_configs,
        providers=providers,
    )
