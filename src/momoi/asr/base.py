from abc import ABC, abstractmethod
from dataclasses import dataclass


class ASRError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioInput:
    data: bytes
    format: str


class ASRProvider(ABC):
    @abstractmethod
    async def transcribe(self, audio: AudioInput) -> str:
        """Return recognized text for one complete voice message."""
