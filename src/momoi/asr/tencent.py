import asyncio
import base64
import hashlib
import hmac
import json
import time
from typing import Any

import aiohttp

from .base import ASRError, ASRProvider, AudioInput


_ENDPOINT = "https://asr.tencentcloudapi.com"
_HOST = "asr.tencentcloudapi.com"
_SERVICE = "asr"
_ACTION = "SentenceRecognition"
_VERSION = "2019-06-14"


def _hmac_sha256(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _tc3_headers(
    body: bytes,
    timestamp: int,
    secret_id: str,
    secret_key: str,
    region: str,
) -> dict[str, str]:
    date = time.strftime("%Y-%m-%d", time.gmtime(timestamp))
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_request = "\n".join(
        (
            "POST",
            "/",
            "",
            "content-type:application/json; charset=utf-8",
            f"host:{_HOST}",
            "",
            "content-type;host",
            payload_hash,
        )
    )
    credential_scope = f"{date}/{_SERVICE}/tc3_request"
    string_to_sign = "\n".join(
        (
            "TC3-HMAC-SHA256",
            str(timestamp),
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        )
    )
    secret_date = _hmac_sha256(f"TC3{secret_key}".encode(), date)
    secret_service = _hmac_sha256(secret_date, _SERVICE)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    headers = {
        "Authorization": (
            f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
            f"SignedHeaders=content-type;host, Signature={signature}"
        ),
        "Content-Type": "application/json; charset=utf-8",
        "Host": _HOST,
        "X-TC-Action": _ACTION,
        "X-TC-Version": _VERSION,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-RequestClient": "momoi",
    }
    if region:
        headers["X-TC-Region"] = region
    return headers


class TencentASRProvider(ASRProvider):
    def __init__(
        self,
        *,
        secret_id: str,
        secret_key: str,
        region: str = "",
        engine: str = "16k_zh",
        timeout_seconds: float = 30,
    ) -> None:
        self.secret_id = secret_id.strip()
        self.secret_key = secret_key.strip()
        self.region = region.strip()
        self.engine = engine.strip() or "16k_zh"
        self.timeout_seconds = float(timeout_seconds)
        if not self.secret_id or not self.secret_key:
            raise ValueError("Tencent ASR requires secret_id and secret_key")
        if self.timeout_seconds <= 0:
            raise ValueError("Tencent ASR timeout_seconds must be positive")

    async def transcribe(self, audio: AudioInput) -> str:
        if not audio.data:
            raise ASRError("ASR audio data is empty")
        voice_format = audio.format.strip().lower()
        if not voice_format:
            raise ASRError("ASR audio format is empty")
        payload = {
            "SubServiceType": 2,
            "EngSerViceType": self.engine,
            "SourceType": 1,
            "VoiceFormat": voice_format,
            "Data": base64.b64encode(audio.data).decode("ascii"),
            "DataLen": len(audio.data),
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        timestamp = int(time.time())
        headers = _tc3_headers(
            body,
            timestamp,
            self.secret_id,
            self.secret_key,
            self.region,
        )
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(_ENDPOINT, data=body, headers=headers) as response:
                    if response.status >= 400:
                        raise ASRError(f"Tencent ASR returned HTTP {response.status}")
                    result: Any = await response.json()
        except ASRError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            raise ASRError(
                f"Tencent ASR request failed: {type(error).__name__}"
            ) from error
        response_data = result.get("Response") if isinstance(result, dict) else None
        if not isinstance(response_data, dict):
            raise ASRError("Tencent ASR returned an invalid response")
        error_data = response_data.get("Error")
        if isinstance(error_data, dict):
            code = str(error_data.get("Code") or "Unknown")
            raise ASRError(f"Tencent ASR rejected the request: {code}")
        text = str(response_data.get("Result") or "").strip()
        if not text:
            raise ASRError("Tencent ASR returned an empty result")
        return text
