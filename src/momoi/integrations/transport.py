from contextlib import asynccontextmanager

import aiohttp


class HTTPTransport:
    """Own a shared pool during application execution; standalone calls also work.

    Authentication is always supplied per request, never as session defaults.
    This prevents credentials leaking between capabilities sharing the pool.
    """

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *_exc):
        if self._session is not None:
            await self._session.close()
            self._session = None

    @asynccontextmanager
    async def session(self, *, timeout_seconds: float):
        if self._session is not None:
            yield self._session
        else:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout_seconds)
            ) as session:
                yield session
