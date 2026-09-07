from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..errors import IntegrationError


class ASRError(IntegrationError):
    pass


@dataclass(frozen=True)
class AudioInput:
    data: bytes
    format: str


class ASRProvider(ABC):
    max_audio_bytes: int = 3 * 1024 * 1024

    @abstractmethod
    async def transcribe(self, audio: AudioInput) -> str:
        """Return recognized text for one complete voice message."""
