import asyncio
import logging

from aiohttp import web

from .app import create_dashboard_app
from ..extensions.base import UsagePlugin
from ..observability.events import log_event
from ..storage import Store

logger = logging.getLogger(__name__)


class DashboardService:
    def __init__(
        self,
        store: Store,
        host: str = "0.0.0.0",
        port: int = 8788,
        *,
        token: str = "",
        usage_plugin: UsagePlugin | None = None,
    ) -> None:
        self.store = store
        self.host = host
        self.port = port
        self.token = token
        self.usage_plugin = usage_plugin

    async def run(self, stop: asyncio.Event) -> None:
        runner = web.AppRunner(
            create_dashboard_app(
                self.store, token=self.token, usage_plugin=self.usage_plugin
            ),
            access_log=None,
        )
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        try:
            await site.start()
            log_event(
                logger,
                logging.INFO,
                "dashboard_start",
                host=self.host,
                port=self.port,
            )
            await stop.wait()
        finally:
            await runner.cleanup()
