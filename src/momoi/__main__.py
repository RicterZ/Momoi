"""Application startup shared by the console launcher and desktop host."""
import argparse
import asyncio
import logging
from importlib.metadata import version
from pathlib import Path
from zoneinfo import ZoneInfo

from .config.loading import load_config
from .config.models import ConfigError
from .observability.events import log_event
from .observability.formatting import configure_logging
from .platform.signals import shutdown_signals
from .runtime import MomoiDaemon


async def run(
    config_path: str | Path,
    *,
    dashboard: bool = True,
    dashboard_host: str = "0.0.0.0",
    dashboard_port: int = 8788,
    stop: asyncio.Event | None = None,
    announce_token: bool = True,
    dashboard_ready: asyncio.Event | None = None,
) -> None:
    if not 1 <= dashboard_port <= 65535:
        raise ValueError("dashboard port must be between 1 and 65535")
    config_path = Path(config_path).expanduser().resolve()
    from .config.workspace import bootstrap

    fresh = bootstrap(config_path) if dashboard else False
    if dashboard:
        from .config.manager import ConfigurationManager

        configuration = ConfigurationManager(config_path)
        config = configuration.dashboard_config()
        if fresh and announce_token:
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
    stop = stop if stop is not None else asyncio.Event()
    with shutdown_signals(stop):
        await _run(config, configuration if dashboard else None, dashboard, dashboard_host, dashboard_port, stop, dashboard_ready)


async def _run(config, configuration, dashboard, dashboard_host, dashboard_port, stop, dashboard_ready):
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
    from .dashboard.service import DashboardService
    from .dashboard.settings import DashboardSettings
    from .runtime.supervisor import RuntimeSupervisor
    from .storage import Store

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
            group.create_task(service.run(stop, dashboard_ready))
            group.create_task(runtime.run(stop))
    finally:
        store.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the headless Momoi daemon")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {version('momoi')}"
    )
    parser.add_argument(
        "--workspace",
        type=lambda value: Path(value).expanduser(),
        default=Path.home() / ".momoi",
        help="runtime workspace (default: ~/.momoi)",
    )
    parser.add_argument("command", nargs="?", choices=["run"], help=argparse.SUPPRESS)
    parser.add_argument(
        "--dashboard",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="serve the Web dashboard (enabled by default; use --no-dashboard for headless mode)",
    )
    parser.add_argument(
        "--dashboard-host",
        default="0.0.0.0",
        help="dashboard bind host (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8788,
        help="dashboard bind port (default: 8788)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run(args.workspace / "config.json", dashboard=args.dashboard,
                        dashboard_host=args.dashboard_host, dashboard_port=args.dashboard_port))
    except (ConfigError, ValueError, OSError) as error:
        raise SystemExit(f"configuration error: {error}") from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
