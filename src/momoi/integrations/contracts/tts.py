from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..errors import IntegrationError


class TTSError(IntegrationError):
    pass


@dataclass(frozen=True)
class AudioOutput:
    data: bytes
    format: str


class TTSProvider(ABC):
    @abstractmethod
    async def synthesize(self, text: str) -> AudioOutput:
        """Synthesize the complete text in memory. Raise TTSError on failure."""
