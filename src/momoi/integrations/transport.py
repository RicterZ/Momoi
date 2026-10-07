from contextlib import asynccontextmanager
import sys
import ssl

import aiohttp
import certifi


def client_tls_context():
    context = ssl.create_default_context()
    # Keep OS/custom trust roots and supplement clean Windows installations.
    context.load_verify_locations(cafile=certifi.where())
    return context


class HTTPTransport:
    """Own a shared pool during application execution; standalone calls also work.

    Authentication is always supplied per request, never as session defaults.
    This prevents credentials leaking between capabilities sharing the pool.
    """

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            trust_env=sys.platform == "win32",
            connector=aiohttp.TCPConnector(ssl=client_tls_context()),
        )
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
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                trust_env=sys.platform == "win32",
                connector=aiohttp.TCPConnector(ssl=client_tls_context()),
            ) as session:
                yield session
