"""Keep the control plane alive while replacing complete runtime generations."""

import asyncio
import logging

from ..channel.weixin.config import WeixinState

logger = logging.getLogger(__name__)


class RuntimeSupervisor:
    def __init__(self, configuration, *, factory=None):
        if factory is None:
            from .daemon import MomoiDaemon

            factory = MomoiDaemon
        self.configuration = configuration
        self.factory = factory
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.daemon = None
        self.task = None
        self.stop = None
        self.applied_revision = ""
        self.observed_revision = ""
        self.state = "setup"
        self.error = ""
        self.missing = []
        self.active_config = None
        self.suspended = False
        self.last_config = None

    def status(self):
        return {
            "state": self.state,
            "error": self.error,
            "missing": self.missing,
            "runtime_active": self.task is not None and not self.task.done(),
            "applied_revision": self.applied_revision,
            "observed_revision": self.observed_revision,
            "saved_revision": self.configuration.revision(),
        }

    def request_apply(self):
        self.changed.set()

    async def pause(self):
        self.suspended = True
        async with self.lock:
            await self._retire()
            self.state = "login"

    def resume(self):
        self.suspended = False
        self.request_apply()

    async def balance(self):
        # Lock against retirement while the dashboard is querying the current provider.
        async with self.lock:
            provider = self.daemon.services.balance if self.daemon is not None else None
            return (
                await provider.balance()
                if provider is not None
                else {"source": "disabled"}
            )

    async def _retire(self):
        if self.task is not None:
            self.stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=30)
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            except Exception:
                pass
        elif self.daemon is not None:
            self.daemon.store.close()
        self.daemon = self.task = self.stop = None

    async def _launch(self, config):
        self.daemon = self.factory(config)
        self.daemon.services.balance
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(self.daemon.run(self.stop))
        ready = asyncio.create_task(self.daemon.ready.wait())
        try:
            done, _ = await asyncio.wait(
                {ready, self.task}, timeout=60, return_when=asyncio.FIRST_COMPLETED
            )
            if self.task in done:
                await self.task
                raise RuntimeError("runtime stopped during startup")
            if ready not in done:
                raise TimeoutError("runtime startup timed out")
        finally:
            ready.cancel()
            await asyncio.gather(ready, return_exceptions=True)

    async def apply(self):
        async with self.lock:
            revision = self.configuration.revision()
            self.observed_revision = revision
            try:
                config = self.configuration.validate()
                if self.active_config is not None:
                    # The persistent dashboard owns these resources for this process.
                    for field in (
                        "database",
                        "thinking",
                        "timezone",
                        "dashboard",
                        "soul_prompt_path",
                        "heartbeat_prompt_path",
                    ):
                        if getattr(config, field) != getattr(self.active_config, field):
                            raise ValueError(
                                f"{field} changed; restart the dashboard process to apply"
                            )
            except Exception as error:
                self.error = (
                    str(error)
                    if isinstance(error, ValueError)
                    else type(error).__name__
                )
                self.state = "error"
                return
            self.missing = []
            if not config.providers.enabled("llm"):
                self.missing.append("llm")
            if not config.channel_configs:
                self.missing.append("channel")
            for item in config.channel_configs:
                if getattr(item, "plugin", "") == "weixin":
                    try:
                        if WeixinState.load(item.state_path) is None:
                            self.missing.append("weixin_login")
                    except ValueError:
                        self.missing.append("weixin_login")
            self.state, self.error = "applying", ""
            logging.getLogger().setLevel(
                getattr(logging, config.log_level, logging.INFO)
            )
            await self._retire()
            if self.missing:
                self.state = "setup"
                self.applied_revision = revision
                self.last_config = None
                return
            try:
                await self._launch(config)
                self.state = "running"
                self.applied_revision = revision
                self.last_config = config
            except Exception as error:
                self.state = "error"
                self.error = f"runtime startup failed: {type(error).__name__}"
                await self._retire()
                if self.last_config is not None:
                    try:
                        await self._launch(self.last_config)
                        self.error += "; previous configuration restored"
                    except Exception as rollback_error:
                        self.error += (
                            f"; recovery failed: {type(rollback_error).__name__}"
                        )
                        await self._retire()

    async def run(self, stop):
        try:
            while not stop.is_set():
                if not self.suspended and (
                    self.changed.is_set()
                    or self.configuration.revision() != self.observed_revision
                ):
                    self.changed.clear()
                    await self.apply()
                if (
                    self.task is not None
                    and self.task.done()
                    and self.state == "running"
                ):
                    self.state = "error"
                    try:
                        await self.task
                    except Exception as error:
                        self.error = f"runtime stopped: {type(error).__name__}"
                    else:
                        self.error = "runtime stopped unexpectedly"
                try:
                    await asyncio.wait_for(stop.wait(), 1)
                except TimeoutError:
                    pass
        finally:
            async with self.lock:
                await self._retire()
