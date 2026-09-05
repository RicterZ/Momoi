import asyncio
import json
import logging
import math
from urllib.parse import urlsplit

import aiohttp

from .base import AudioOutput, TTSError, TTSProvider
from ..observability.events import log_event


FISH_MODELS = frozenset({"s1", "s2-pro", "s2.1-pro", "s2.1-pro-free"})
TTS_RETRY_DELAYS = (1, 2, 4)
logger = logging.getLogger(__name__)


class FishAudioTTSProvider(TTSProvider):
    """Fish HTTP TTS with one configured voice and in-memory audio output."""

    def __init__(
        self,
        *,
        api_key: str,
        reference_id: str,
        model: str = "s2.1-pro-free",
        base_url: str = "https://api.fish.audio",
        format: str = "mp3",
        latency: str = "normal",
        timeout_seconds: float = 60,
        max_audio_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        for name, value in {
            "api_key": api_key, "reference_id": reference_id, "model": model,
            "base_url": base_url, "format": format, "latency": latency,
        }.items():
            if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n"):
                raise ValueError(f"Fish TTS {name} must be a nonempty single-line string")
        self.api_key = api_key.strip()
        self.reference_id = reference_id.strip()
        self.model = model.strip()
        # Fish silently falls back to a paid model for unknown model names.
        if self.model not in FISH_MODELS:
            raise ValueError("Fish TTS model must be one of: " + ", ".join(sorted(FISH_MODELS)))
        self.base_url = base_url.strip().rstrip("/")
        parsed = urlsplit(self.base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Fish TTS base_url must be an absolute HTTP URL without credentials, query or fragment")
        if format not in {"mp3", "wav", "opus"}:
            raise ValueError("Fish TTS format must be mp3, wav or opus")
        if latency not in {"normal", "balanced", "low"}:
            raise ValueError("Fish TTS latency must be normal, balanced or low")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("Fish TTS timeout_seconds must be finite and positive")
        if isinstance(max_audio_bytes, bool) or not isinstance(max_audio_bytes, int) or max_audio_bytes <= 0:
            raise ValueError("Fish TTS max_audio_bytes must be a positive integer")
        self.format = format
        self.latency = latency
        self.timeout_seconds = timeout_seconds
        self.max_audio_bytes = max_audio_bytes

    async def synthesize(self, text: str) -> AudioOutput:
        if not isinstance(text, str) or not text.strip():
            raise TTSError("Fish TTS requires nonempty text")
        attempts = len(TTS_RETRY_DELAYS) + 1
        for attempt in range(1, attempts + 1):
            try:
                return await self._synthesize_once(text)
            except TTSError as error:
                log_event(
                    logger, logging.WARNING, "tts_request_failed",
                    attempt=attempt, attempt_max=attempts,
                    endpoint=f"{self.base_url}/v1/tts",
                    reason=str(error), retry=attempt < attempts,
                )
                if attempt == attempts:
                    raise TTSError(f"{error} (failed after {attempts} attempts)") from error
                await asyncio.sleep(TTS_RETRY_DELAYS[attempt - 1])

    def _error_detail(self, detail: str, text: str) -> str:
        for private in (self.api_key, self.reference_id, text):
            for value in (private, json.dumps(private, ensure_ascii=True)[1:-1],
                          json.dumps(private, ensure_ascii=False)[1:-1]):
                detail = detail.replace(value, "[redacted]")
        return detail[:2000]

    async def _synthesize_once(self, text: str) -> AudioOutput:
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/v1/tts",
                    headers={"Authorization": f"Bearer {self.api_key}", "model": self.model},
                    json={"text": text, "reference_id": self.reference_id,
                          "format": self.format, "latency": self.latency},
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        body = (await response.content.read(8192)).decode("utf-8", errors="replace")
                        detail = self._error_detail(body, text)
                        raise TTSError(f"Fish TTS returned HTTP {response.status}: {detail}")
                    content_type = response.content_type
                    if not (content_type.startswith("audio/") or content_type == "application/octet-stream"):
                        body = (await response.content.read(8192)).decode("utf-8", errors="replace")
                        raise TTSError(
                            f"Fish TTS returned a non-audio response ({content_type}): "
                            f"{self._error_detail(body, text)}"
                        )
                    if response.content_length and response.content_length > self.max_audio_bytes:
                        raise TTSError("Fish TTS audio exceeds max_audio_bytes")
                    audio = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        if len(audio) + len(chunk) > self.max_audio_bytes:
                            raise TTSError("Fish TTS audio exceeds max_audio_bytes")
                        audio.extend(chunk)
                    if not audio:
                        raise TTSError("Fish TTS returned empty audio")
            return AudioOutput(bytes(audio), self.format)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            detail = f"{type(error).__name__}: {error}"
            if isinstance(error, aiohttp.ClientConnectorError):
                detail += f"; host={error.host}; port={error.port}; os_error={error.os_error!r}"
            raise TTSError(f"Fish TTS request failed: {self._error_detail(detail, text)}") from error
