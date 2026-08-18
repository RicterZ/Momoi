import json
import unittest
from unittest.mock import patch

from momoi.asr import ASRProvider, AudioInput, load_asr_provider
from momoi.asr.tencent import TencentASRProvider, _tc3_headers


class StubASRProvider(ASRProvider):
    def __init__(self, *, prefix: str = "") -> None:
        self.prefix = prefix

    async def transcribe(self, audio: AudioInput) -> str:
        return self.prefix + audio.data.decode()


class ASRProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_loads_dotted_provider_with_settings(self) -> None:
        provider = load_asr_provider(f"{__name__}.StubASRProvider", prefix="heard:")
        self.assertIsInstance(provider, StubASRProvider)
        self.assertEqual(
            await provider.transcribe(AudioInput(b"hello", "mp3")),
            "heard:hello",
        )

    async def test_tencent_request_contains_audio_length_and_format(self) -> None:
        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def json(self) -> dict[str, object]:
                return {"Response": {"Result": "测试语音", "RequestId": "request"}}

        class Session:
            def __init__(self) -> None:
                self.body = b""
                self.headers: dict[str, str] = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            def post(
                self, _url: str, *, data: bytes, headers: dict[str, str]
            ) -> Response:
                self.body = data
                self.headers = headers
                return Response()

        session = Session()
        provider = TencentASRProvider(
            secret_id="id",
            secret_key="key",
            region="ap-shanghai",
            engine="16k_zh",
        )
        with (
            patch("momoi.asr.tencent.aiohttp.ClientSession", return_value=session),
            patch("momoi.asr.tencent.time.time", return_value=1_700_000_000),
        ):
            result = await provider.transcribe(AudioInput(b"audio", "mp3"))

        request = json.loads(session.body)
        self.assertEqual(result, "测试语音")
        self.assertEqual(request["DataLen"], 5)
        self.assertEqual(request["VoiceFormat"], "mp3")
        self.assertEqual(request["EngSerViceType"], "16k_zh")
        self.assertEqual(session.headers["X-TC-Region"], "ap-shanghai")
        self.assertNotIn("key", json.dumps(session.headers))

    def test_tc3_signature_is_deterministic(self) -> None:
        headers = _tc3_headers(b"{}", 1_700_000_000, "id", "key", "")
        self.assertEqual(headers["X-TC-Timestamp"], "1700000000")
        self.assertEqual(
            headers["Authorization"],
            (
                "TC3-HMAC-SHA256 "
                "Credential=id/2023-11-14/asr/tc3_request, "
                "SignedHeaders=content-type;host, "
                "Signature="
                "6bb0c2c55412d6812b0afcd39a55be88041d2b672e47a14345d66d6bdaa1c935"
            ),
        )
