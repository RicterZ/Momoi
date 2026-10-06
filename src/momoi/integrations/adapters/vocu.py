"""Vocu / 悟声 synchronous TTS and streaming MP3-to-PCM conversion.

Protocol: https://dev.wusound.cn/同步实时生成语音-api-380467873
"""

import asyncio
import json
from contextlib import aclosing
from urllib.parse import urlsplit

import aiohttp

from ..contracts.tts import AudioOutput, TTSError, TTSProvider
from ..errors import ErrorCategory, error_category, http_category
from ..transport import HTTPTransport
from ..validation import number, text as option_text, url


class VocuTTSProvider(TTSProvider):
    def __init__(
        self, *, api_key: str, voice_id: str,
        base_url: str = "https://v1.wusound.cn/api", prompt_id: str = "default",
        preset: str = "balance", speech_rate: float = 1, language: str = "auto",
        flash: bool = False, vivid: bool = False, timeout_seconds: float = 60,
        max_audio_bytes: int = 20 * 1024 * 1024,
        transport: HTTPTransport | None = None,
    ):
        options = locals()
        for key in ("api_key", "voice_id", "prompt_id", "preset", "language"):
            setattr(self, key, option_text(options, key))
        if self.preset not in {"creative", "balance", "stable"}:
            raise ValueError("preset must be creative, balance or stable")
        self.base_url = url(options, "base_url")
        self.speech_rate = number(options, "speech_rate", 1)
        if not .5 <= self.speech_rate <= 2:
            raise ValueError("speech_rate must be between 0.5 and 2")
        for key in ("flash", "vivid"):
            if type(options[key]) is not bool:
                raise ValueError(f"{key} must be boolean")
            setattr(self, key, options[key])
        self.timeout_seconds = number(options, "timeout_seconds", 60)
        self.max_audio_bytes = number(options, "max_audio_bytes", 20971520, integer=True)
        self.transport = transport or HTTPTransport()

    def _error(self, detail, *, category=ErrorCategory.INVALID_RESPONSE):
        return TTSError(detail, category=category, service="vocu", operation="synthesize")

    async def _audio_chunks(self, text: str, *, streaming: bool):
        if not isinstance(text, str) or not text.strip():
            raise self._error("Vocu TTS requires nonempty text", category=ErrorCategory.REQUEST)
        payload = {
            "voiceId": self.voice_id, "text": text, "promptId": self.prompt_id,
            "preset": self.preset, "speechRate": self.speech_rate,
            "language": self.language, "flash": self.flash, "vivid": self.vivid,
            "stream": streaming, "srt": False,
        }
        try:
            async with self.transport.session(timeout_seconds=self.timeout_seconds) as session:
                timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
                async with session.post(
                    self.base_url + "/tts/simple-generate", json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    allow_redirects=False, timeout=timeout,
                ) as response:
                    if response.status != 200:
                        raise self._error(f"Vocu TTS returned HTTP {response.status}",
                                          category=http_category(response.status))
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        raw.extend(chunk)
                        if len(raw) > 65536:
                            raise self._error("Vocu TTS response exceeds metadata limit")
                    try:
                        result = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        raise self._error("Vocu TTS returned invalid JSON") from None
                    if not isinstance(result, dict) or result.get("status") != 200:
                        raise self._error("Vocu TTS generation failed")
                    data = result.get("data")
                    if not isinstance(data, dict):
                        raise self._error("Vocu TTS response is missing audio metadata")
                    audio_url = (data.get("streamUrl") or data.get("audio")) if streaming else data.get("audio")
                    parsed = urlsplit(audio_url) if isinstance(audio_url, str) else None
                    if (parsed is None or parsed.scheme not in {"http", "https"}
                            or not parsed.hostname or parsed.username or parsed.password or parsed.fragment):
                        raise self._error("Vocu TTS returned an invalid audio URL")
                # Signed CDN URLs supply their own authorization. Never send the API key here.
                async with session.get(audio_url, timeout=timeout) as response:
                    if response.status != 200:
                        raise self._error(f"Vocu audio download returned HTTP {response.status}",
                                          category=http_category(response.status))
                    if not (response.content_type.startswith("audio/")
                            or response.content_type == "application/octet-stream"):
                        raise self._error("Vocu audio download returned non-audio data")
                    if response.content_length and response.content_length > self.max_audio_bytes:
                        raise self._error("Vocu audio exceeds max_audio_bytes")
                    total = 0
                    async for chunk in response.content.iter_chunked(4096):
                        total += len(chunk)
                        if total > self.max_audio_bytes:
                            raise self._error("Vocu audio exceeds max_audio_bytes")
                        yield chunk
                    if not total:
                        raise self._error("Vocu TTS returned empty audio")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            # URLs and server bodies may contain signed tokens, API keys or input text.
            raise self._error(f"Vocu TTS request failed: {type(error).__name__}",
                              category=error_category(error)) from error

    async def synthesize(self, text: str) -> AudioOutput:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with aclosing(self._audio_chunks(text, streaming=False)) as stream:
                    return AudioOutput(b"".join([chunk async for chunk in stream]), "mp3")
        except TimeoutError:
            raise self._error("Vocu TTS timed out", category=ErrorCategory.TIMEOUT) from None

    async def stream_pcm(self, text: str):
        """Decode streamed MP3 into 24kHz mono s16le; no synthesis retries."""
        process = None
        writer = None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                process = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-probesize", "32768", "-analyzeduration", "0",
                    "-f", "mp3", "-i", "pipe:0", "-f", "s16le",
                    "-ar", "24000", "-ac", "1", "pipe:1",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )

                async def feed():
                    try:
                        async with aclosing(self._audio_chunks(text, streaming=True)) as stream:
                            async for chunk in stream:
                                process.stdin.write(chunk)
                                await process.stdin.drain()
                    finally:
                        process.stdin.close()

                writer = asyncio.create_task(feed())
                total = 0
                pending = b""
                while chunk := await process.stdout.read(4096):
                    total += len(chunk)
                    if total > min(self.max_audio_bytes, 48000 * 120):
                        raise self._error("Vocu decoded PCM exceeds audio limit")
                    pending += chunk
                    size = len(pending) // 2 * 2
                    if size:
                        yield pending[:size]
                        pending = pending[size:]
                await writer
                if await process.wait() != 0 or not total or pending:
                    raise self._error("Vocu MP3 decoding failed or returned empty PCM")
        except FileNotFoundError:
            raise self._error("Vocu telephone streaming requires ffmpeg on PATH",
                              category=ErrorCategory.REQUEST) from None
        except (OSError, TimeoutError) as error:
            raise self._error(f"Vocu streaming TTS failed: {type(error).__name__}",
                              category=error_category(error)) from error
        finally:
            if writer is not None:
                writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
