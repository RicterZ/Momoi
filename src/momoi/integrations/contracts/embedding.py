from typing import Protocol
from ..models import EmbeddingSpaceConfig


class Embedder(Protocol):
    space: EmbeddingSpaceConfig

    async def encode(self, texts: list[str], *, query: bool) -> list[list[float]]: ...
    async def health(self) -> tuple[bool, float, str]: ...
    async def close(self) -> None: ...
