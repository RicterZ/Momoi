import asyncio
import logging
import signal
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config.loading import load_config
from ..observability.events import log_event
from ..observability.formatting import configure_logging
from ..runtime import MomoiDaemon


async def run(
    config_path: str | Path,
    *,
    dashboard: bool = True,
    dashboard_host: str = "0.0.0.0",
    dashboard_port: int = 8788,
) -> None:
    if not 1 <= dashboard_port <= 65535:
        raise ValueError("dashboard port must be between 1 and 65535")
    config_path = Path(config_path).expanduser().resolve()
    from ..config.workspace import bootstrap

    fresh = bootstrap(config_path) if dashboard else False
    if dashboard:
        from ..config.manager import ConfigurationManager

        configuration = ConfigurationManager(config_path)
        config = configuration.dashboard_config()
        if fresh:
            print(f"Dashboard access token: {config.dashboard.token}", flush=True)
    else:
        config = load_config(config_path)
    if dashboard and not config.dashboard.token:
        raise ValueError("dashboard.token is required when --dashboard is enabled")
    configure_logging(
        getattr(logging, config.log_level, logging.INFO),
        ZoneInfo(config.timezone),
    )
    for noisy_logger in ("httpx", "httpcore", "mcp"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        if sig := getattr(signal, name, None):
            loop.add_signal_handler(sig, stop.set)
    log_event(
        logging.getLogger(__name__),
        logging.INFO,
        "service_start",
        model_adapter=config.providers.adapter_for("llm"),
        channels=",".join(
            str(getattr(item, "plugin", "unknown")) for item in config.channel_configs
        ),
        primary_channel=getattr(config.channel, "plugin", "unknown"),
        soul_prompt_path=str(config.soul_prompt_path or ""),
        soul_prompt_chars=len(config.soul_prompt),
        heartbeat_prompt_path=str(config.heartbeat_prompt_path or ""),
        heartbeat_prompt_chars=len(config.heartbeat_prompt),
    )
    if not dashboard:
        await MomoiDaemon(config).run(stop)
        return
    from ..dashboard.service import DashboardService
    from ..dashboard.settings import DashboardSettings
    from ..runtime.supervisor import RuntimeSupervisor
    from ..storage import Store

    runtime = RuntimeSupervisor(configuration)
    runtime.active_config = config
    store = Store(
        config.database,
        config.workspace,
        thinking=config.thinking,
        timezone=config.timezone,
    )
    service = DashboardService(
        store,
        dashboard_host,
        dashboard_port,
        token=config.dashboard.token,
        settings=DashboardSettings.from_config(config),
        balance_provider=runtime,
        configuration=configuration,
        runtime=runtime,
    )
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(service.run(stop))
            group.create_task(runtime.run(stop))
    finally:
        store.close()
