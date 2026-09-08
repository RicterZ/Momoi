from typing import Any, Callable, Protocol

from ...models import ProviderResponse


class LanguageModel(Protocol):
    usage_sink: Callable[..., None] | None
    thinking_sink: Callable[..., None] | None
    usage_parser: Callable[..., Any] | None

    async def complete(
        self,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        require_tool: bool = False,
        required_tool: str | None = None,
    ) -> ProviderResponse: ...
