import math

from .models import ConfigError, TTSConfig
from .validation import boolean, integer, mapping
from ..tts.fish import FishAudioTTSProvider


def load_tts_config(value: object) -> TTSConfig:
    raw = mapping(value, "tts")
    unknown = set(raw) - {"enabled", "provider", "timeout_seconds", "max_audio_bytes", "settings"}
    if unknown:
        raise ConfigError(f"unknown tts field: {sorted(unknown)[0]}")
    enabled = boolean(raw.get("enabled", False), "tts.enabled")
    provider = raw.get("provider", "fish")
    if provider != "fish":
        raise ConfigError("tts.provider must be fish")
    timeout = raw.get("timeout_seconds", 60)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ConfigError("tts.timeout_seconds must be finite and positive")
    max_audio = integer(raw.get("max_audio_bytes", 20 * 1024 * 1024), "tts.max_audio_bytes", minimum=1)
    settings = dict(mapping(raw.get("settings", {}), "tts.settings"))
    unknown = set(settings) - {"api_key", "reference_id", "model", "base_url", "format", "latency"}
    if unknown:
        raise ConfigError(f"unknown tts.settings field: {sorted(unknown)[0]}")
    validated = dict(settings)
    for key in ("api_key", "reference_id"):
        if not enabled and not validated.get(key):
            validated[key] = "disabled"
        elif key not in validated:
            raise ConfigError(f"tts.settings.{key} is required when TTS is enabled")
    try:
        FishAudioTTSProvider(
            **validated,
            timeout_seconds=timeout, max_audio_bytes=max_audio,
        )
    except (TypeError, ValueError) as error:
        raise ConfigError(f"invalid tts configuration: {error}") from None
    return TTSConfig(
        enabled=enabled, provider=provider, timeout_seconds=timeout,
        max_audio_bytes=max_audio, settings=settings or None,
    )
