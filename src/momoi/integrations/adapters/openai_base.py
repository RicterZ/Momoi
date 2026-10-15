"""Shared lifecycle for OpenAI-protocol LLM adapters.

The wire protocols (Chat Completions and Responses) are separate adapters,
but they share configuration, transport state, and observability sinks.
"""

from pathlib import Path
from typing import Callable

import aiohttp

from ...integrations.models import LLMConfig
from ...models import ProviderResponse
from ...llm.streaming import StreamingProvider
from ...llm.transport import ProviderCircuit


class OpenAIProtocolProvider(StreamingProvider):
    """Base class for OpenAI-compatible protocol implementations."""

    protocol = "openai"

    def __init__(self, config: LLMConfig, dump_dir: Path | None = None) -> None:
        self.config = config
        self._circuit = ProviderCircuit()
        self.dump_dir = dump_dir
        self.request_metrics_sink = None
        self.usage_sink: Callable[..., None] | None = None
        self.usage_parser: Callable[[dict], dict | None] | None = None
        self.thinking_sink: Callable[..., None] | None = None
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._session:
            await self._session.close()

    async def complete(self, *args, **kwargs) -> ProviderResponse:
        raise NotImplementedError
