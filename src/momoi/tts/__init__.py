from .base import AudioOutput, TTSError, TTSProvider
from .fish import FishAudioTTSProvider
from ..config.models import AppConfig


def create_tts_provider(config: AppConfig) -> TTSProvider | None:
    if not config.tts.enabled:
        return None
    if config.tts.provider != "fish":
        raise ValueError("tts.provider must be fish")
    return FishAudioTTSProvider(
        **(config.tts.settings or {}),
        timeout_seconds=config.tts.timeout_seconds,
        max_audio_bytes=config.tts.max_audio_bytes,
    )


__all__ = ["AudioOutput", "TTSError", "TTSProvider", "FishAudioTTSProvider", "create_tts_provider"]
