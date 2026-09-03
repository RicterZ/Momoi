import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .anthropic import AnthropicProvider
from .openai import OpenAIProvider
from ..config.models import LLMConfig
from ..models import ProviderResponse

Provider = OpenAIProvider | AnthropicProvider


@dataclass
class _ProviderState:
    provider: Provider
    requests: int = 0
    retired: bool = False


class ProviderManager:
    """Own the active protocol client and retire it after in-flight requests finish."""

    def __init__(self, config: LLMConfig, dump_dir: Path | None = None) -> None:
        self._dump_dir = dump_dir
        self._usage_sink: Callable[..., None] | None = None
        self._usage_parser: (
            Callable[[dict[str, Any]], dict[str, float | int | bool] | None] | None
        ) = None
        self._thinking_sink: Callable[..., None] | None = None
        self._current = _ProviderState(self._create(config))
        self._retired: list[_ProviderState] = []
        self._state_lock = asyncio.Lock()
        self._update_lock = asyncio.Lock()
        self._started = False

    @property
    def config(self) -> LLMConfig:
        return self._current.provider.config

    @property
    def usage_sink(self) -> Callable[..., None] | None:
        return self._usage_sink

    @usage_sink.setter
    def usage_sink(self, value: Callable[..., None] | None) -> None:
        self._usage_sink = value
        self._current.provider.usage_sink = value

    @property
    def usage_parser(
        self,
    ) -> Callable[[dict[str, Any]], dict[str, float | int | bool] | None] | None:
        return self._usage_parser

    @usage_parser.setter
    def usage_parser(
        self,
        value: Callable[[dict[str, Any]], dict[str, float | int | bool] | None] | None,
    ) -> None:
        self._usage_parser = value
        self._current.provider.usage_parser = value

    @property
    def thinking_sink(self) -> Callable[..., None] | None:
        return self._thinking_sink

    @thinking_sink.setter
    def thinking_sink(self, value: Callable[..., None] | None) -> None:
        self._thinking_sink = value
        self._current.provider.thinking_sink = value

    async def __aenter__(self) -> "ProviderManager":
        async with self._update_lock:
            await self._current.provider.__aenter__()
            self._started = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        async with self._update_lock:
            self._started = False
            async with self._state_lock:
                states = [self._current, *self._retired]
                self._retired.clear()
            for state in states:
                await state.provider.__aexit__(*exc)

    async def complete(
        self,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        require_tool: bool = False,
    ) -> ProviderResponse:
        async with self._state_lock:
            state = self._current
            state.requests += 1
        try:
            return await state.provider.complete(
                system,
                messages,
                tools,
                require_tool=require_tool,
            )
        finally:
            await self._release(state)

    async def update_config(self, config: LLMConfig) -> None:
        async with self._update_lock:
            current = self._current
            if current.provider.config.api_format == config.api_format:
                current.provider.update_config(config)
                return

            replacement = self._create(config)
            installed = False
            try:
                if self._started:
                    await replacement.__aenter__()

                close: Provider | None = None
                async with self._state_lock:
                    previous = self._current
                    self._current = _ProviderState(replacement)
                    installed = True
                    previous.retired = True
                    if previous.requests:
                        self._retired.append(previous)
                    elif self._started:
                        close = previous.provider
            except BaseException:
                if self._started and not installed:
                    await replacement.__aexit__()
                raise
            if close is not None:
                await close.__aexit__()

    async def _release(self, state: _ProviderState) -> None:
        close: Provider | None = None
        async with self._state_lock:
            state.requests -= 1
            if state.retired and not state.requests:
                if state in self._retired:
                    self._retired.remove(state)
                if self._started:
                    close = state.provider
        if close is not None:
            await close.__aexit__()

    def _create(self, config: LLMConfig) -> Provider:
        if config.api_format == "openai":
            provider: Provider = OpenAIProvider(config, self._dump_dir)
        elif config.api_format == "anthropic":
            provider = AnthropicProvider(config, self._dump_dir)
        else:
            raise ValueError("llm.api_format must be anthropic or openai")
        self._configure(provider)
        return provider

    def _configure(self, provider: Provider) -> None:
        provider.usage_sink = self._usage_sink
        provider.usage_parser = self._usage_parser
        provider.thinking_sink = self._thinking_sink
