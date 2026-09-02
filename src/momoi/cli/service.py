import asyncio
import logging
import signal
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config.loading import load_config
from ..logging_context import configure_logging, log_event
from ..runtime import MomoiDaemon


async def run(
    config_path: str | Path,
    *,
    dashboard: bool = False,
    dashboard_host: str = "0.0.0.0",
    dashboard_port: int = 8788,
) -> None:
    if not 1 <= dashboard_port <= 65535:
        raise ValueError("dashboard port must be between 1 and 65535")
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
        model=config.llm.model,
        channels=",".join(
            str(getattr(item, "plugin", "unknown")) for item in config.channel_configs
        ),
        primary_channel=getattr(config.channel, "plugin", "unknown"),
        soul_prompt_path=str(config.soul_prompt_path or ""),
        soul_prompt_chars=len(config.soul_prompt),
        heartbeat_prompt_path=str(config.heartbeat_prompt_path or ""),
        heartbeat_prompt_chars=len(config.heartbeat_prompt),
    )
    dashboard_bind = (dashboard_host, dashboard_port) if dashboard else None
    await MomoiDaemon(config, dashboard=dashboard_bind).run(stop)
