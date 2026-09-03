import json
from importlib.resources import files
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..channel import load_channel_config
from .environment import apply_env_overrides
from .models import (
    ASRConfig,
    AppConfig,
    AutonomyConfig,
    ConfigError,
    DashboardConfig,
    EmbeddingConfig,
    EpisodeAnnealingConfig,
    HeartbeatConfig,
    LLMConfig,
    NotificationConfig,
    ReflectionConfig,
    ThinkingConfig,
    UsageConfig,
    WebhookConfig,
)
from .validation import boolean, clock, integer, mapping, nonnegative, positive


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as file:
        raw = json.load(file)
    if not isinstance(raw, dict):
        raise ConfigError("config.json must be a table/object")
    apply_env_overrides(raw)

    llm_raw = mapping(raw.get("llm"), "llm")
    model = str(llm_raw.get("model") or "")
    api_key = str(llm_raw.get("api_key") or "")
    if not api_key:
        raise ConfigError("llm.api_key is required")
    if not model:
        raise ConfigError("llm.model is required")
    api_format = str(llm_raw.get("api_format", "anthropic")).lower()
    if api_format not in {"anthropic", "openai"}:
        raise ConfigError("llm.api_format must be anthropic or openai")
    tool_choice = boolean(llm_raw.get("tool_choice", True), "llm.tool_choice")
    thinking_raw = mapping(llm_raw.get("thinking") or {}, "llm.thinking")
    thinking_effort = str(thinking_raw.get("effort") or "").lower()
    allowed_thinking_efforts = {"", "low", "high", "max"}
    if thinking_effort not in allowed_thinking_efforts:
        raise ConfigError("llm.thinking.effort must be low, high, or max")
    raw_thinking_stages = mapping(
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
        raise ConfigError("llm.thinking.stages values must be low, high, or max")

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
    usage_raw = mapping(raw.get("usage", {}), "usage")
    usage_settings = {
        key: value
        for key, value in usage_raw.items()
        if key not in {"provider", "api_key"}
    }
    asr_raw = mapping(raw.get("asr", {}), "asr")
    asr_enabled = boolean(asr_raw.get("enabled", False), "asr.enabled")
    asr_provider = str(asr_raw.get("provider") or "tencent").strip()
    if not asr_provider:
        raise ConfigError("asr.provider must not be empty")
    asr_settings = mapping(asr_raw.get("settings", {}), "asr.settings")
    asr_timeout = positive(asr_raw.get("timeout_seconds", 30), "asr.timeout_seconds")
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
    heartbeat_raw = mapping(raw.get("heartbeat", {}), "heartbeat")
    autonomy_raw = mapping(raw.get("autonomy", {}), "autonomy")
    reflection_raw = mapping(raw.get("reflection", {}), "reflection")
    annealing_raw = mapping(raw.get("episode_annealing", {}), "episode_annealing")
    embedding_raw = mapping(raw.get("embedding", {}), "embedding")
    embedding_enabled = boolean(
        embedding_raw.get("enabled", False), "embedding.enabled"
    )
    embedding_model = str(embedding_raw.get("model", "BAAI/bge-small-zh-v1.5")).strip()
    embedding_dimensions = int(embedding_raw.get("dimensions", 512))
    embedding_profile = str(
        embedding_raw.get("calibration_profile", "bge-small-zh-v1.5-momoi-v1")
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
    allowed_tools = autonomy_raw.get(
        "allowed_tools", ["curl", "read_file", "write_file", "list_dir"]
    )
    if not isinstance(allowed_tools, list) or not all(
        isinstance(item, str) and item.strip() for item in allowed_tools
    ):
        raise ConfigError("autonomy.allowed_tools must be an array of tool names")
    max_input_tokens = integer(
        context_raw.get("max_input_tokens", 142222),
        "context.max_input_tokens",
        minimum=1000,
    )
    context_compaction_ratio = float(context_raw.get("context_compaction_ratio", 0.9))
    if not 0 < context_compaction_ratio <= 1:
        raise ConfigError("context.context_compaction_ratio must be between 0 and 1")
    transcript_turns_min = integer(
        context_raw.get("transcript_turns_min", 48),
        "context.transcript_turns_min",
        minimum=1,
    )
    transcript_turns_max = integer(
        context_raw.get("transcript_turns_max", 96),
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
    max_retries = integer(
        llm_raw.get("max_retries", 3),
        "llm.max_retries",
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
        llm=LLMConfig(
            base_url=str(llm_raw["base_url"]).rstrip("/"),
            api_key=api_key,
            model=model,
            max_tokens=int(llm_raw.get("max_tokens", 16384)),
            temperature=float(llm_raw.get("temperature", 0.6)),
            timeout_seconds=positive(
                llm_raw.get("timeout_seconds", 300), "llm.timeout_seconds"
            ),
            max_retries=max_retries,
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
        usage=UsageConfig(
            provider=str(usage_raw.get("provider") or ""),
            api_key=str(usage_raw.get("api_key") or ""),
            settings=usage_settings or None,
        ),
        heartbeat=HeartbeatConfig(
            enabled=boolean(heartbeat_raw.get("enabled", False), "heartbeat.enabled"),
            initial_delay_seconds=positive(
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
                embedding_raw.get("endpoint", "http://embedding:8002/v1/embeddings")
            ).rstrip("/"),
            api_key=str(embedding_raw.get("api_key") or ""),
            model=embedding_model,
            dimensions=embedding_dimensions,
            calibration_profile=embedding_profile,
            query_timeout_seconds=positive(
                embedding_raw.get("query_timeout_seconds", 5),
                "embedding.query_timeout_seconds",
            ),
            document_timeout_seconds=positive(
                embedding_raw.get("document_timeout_seconds", 30),
                "embedding.document_timeout_seconds",
            ),
            document_batch_size=document_batch_size,
        ),
        config_path=config_path,
    )
