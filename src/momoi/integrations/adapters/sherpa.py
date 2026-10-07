"""Optional local CPU ASR, in process or through the private ASR container."""
import asyncio
import io
import json
from pathlib import Path
import threading
import wave

import aiohttp
import numpy as np

from ..contracts.asr import ASRError, ASRProvider, AudioInput


class SherpaEngine:
    def __init__(self, model_path, num_threads=2, trailing_silence=1.2):
        import sherpa_onnx
        root = Path(model_path)
        names = ('tokens.txt', 'encoder.int8.onnx', 'decoder.onnx', 'joiner.int8.onnx')
        if any(not (root / name).is_file() for name in names):
            raise ValueError('本地 ASR 模型不完整：' + str(root))
        self.lock = threading.Lock()
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(root / names[0]), encoder=str(root / names[1]),
            decoder=str(root / names[2]), joiner=str(root / names[3]),
            num_threads=num_threads, provider='cpu', decoding_method='greedy_search',
            enable_endpoint_detection=True, rule1_min_trailing_silence=2.4,
            rule2_min_trailing_silence=trailing_silence, rule3_min_utterance_length=20)

    def create_stream(self):
        with self.lock:
            return self.recognizer.create_stream()

    def feed(self, stream, pcm, *, finish=False, sample_rate=16000):
        if len(pcm) % 2:
            raise ValueError('PCM 必须是 16-bit 单声道')
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        with self.lock:
            stream.accept_waveform(sample_rate, samples)
            if finish:
                stream.accept_waveform(sample_rate, np.zeros(round(sample_rate * .3), dtype=np.float32))
                stream.input_finished()
            while self.recognizer.is_ready(stream):
                self.recognizer.decode_stream(stream)
            text = self.recognizer.get_result(stream).strip()
            final = finish or self.recognizer.is_endpoint(stream)
            if final and not finish:
                self.recognizer.reset(stream)
            return {'text': text, 'final': final}

    def transcribe(self, data):
        with wave.open(io.BytesIO(data)) as wav:
            if (wav.getnchannels(), wav.getsampwidth()) != (1, 2):
                raise ValueError('本地 ASR 需要 16-bit、单声道 WAV')
            sample_rate = wav.getframerate()
            pcm = wav.readframes(wav.getnframes())
        return self.feed(self.create_stream(), pcm, finish=True, sample_rate=sample_rate)['text']


class LocalStream:
    def __init__(self, engine, stream):
        self.engine, self.stream = engine, stream

    async def feed(self, pcm):
        return await asyncio.to_thread(self.engine.feed, self.stream, pcm)

    async def close(self):
        self.stream = None


class RemoteStream:
    def __init__(self, ws, timeout):
        self.ws, self.timeout = ws, timeout

    async def feed(self, pcm):
        try:
            await self.ws.send_bytes(pcm)
            message = await self.ws.receive(timeout=self.timeout)
            if message.type != aiohttp.WSMsgType.TEXT:
                raise ASRError('本地 ASR 流连接已关闭')
            value = json.loads(message.data)
            if not isinstance(value.get('text'), str) or type(value.get('final')) is not bool:
                raise ASRError('本地 ASR 返回无效流结果')
            return value
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            raise ASRError('本地 ASR 流请求失败') from error

    async def close(self):
        await self.ws.close()


class SherpaASRProvider(ASRProvider):
    engine = 'zipformer-zh-int8-2025-06-30'

    def __init__(self, *, endpoint='', model_path='', num_threads=2,
                 trailing_silence=1.2, timeout_seconds=30):
        if bool(endpoint) == bool(model_path):
            raise ValueError('本地 ASR 必须填写 endpoint 或 model_path，二选一')
        from ..validation import url, number
        if endpoint:
            url({'endpoint': endpoint}, 'endpoint')
        number({'num_threads': num_threads}, 'num_threads', 2, integer=True)
        number({'trailing_silence': trailing_silence}, 'trailing_silence', 1.2)
        number({'timeout_seconds': timeout_seconds}, 'timeout_seconds', 30)
        if num_threads > 16 or trailing_silence > 5:
            raise ValueError('本地 ASR 线程数不能超过 16，断句静音不能超过 5 秒')
        self.endpoint = endpoint.rstrip('/')
        self.model_path, self.num_threads = model_path, num_threads
        self.trailing_silence, self.timeout = trailing_silence, timeout_seconds
        self._engine = None
        self._http = None
        self._load_lock = asyncio.Lock()

    async def _local(self):
        async with self._load_lock:
            if self._engine is None:
                try:
                    self._engine = await asyncio.to_thread(SherpaEngine, self.model_path,
                                                          self.num_threads, self.trailing_silence)
                except (ImportError, ValueError, RuntimeError, OSError) as error:
                    raise ASRError('本地 ASR 加载失败，请安装本地 ASR 组件并检查模型目录') from error
        return self._engine

    def _client(self):
        if self._http is None:
            self._http = aiohttp.ClientSession(trust_env=False,
                timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self._http

    async def create_stream(self):
        if not self.endpoint:
            engine = await self._local()
            return LocalStream(engine, await asyncio.to_thread(engine.create_stream))
        try:
            ws = await self._client().ws_connect(self.endpoint + '/v1/stream', heartbeat=10)
            return RemoteStream(ws, self.timeout)
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise ASRError('无法连接本地 ASR 容器') from error

    async def transcribe(self, audio: AudioInput):
        if audio.format != 'wav' or not audio.data or len(audio.data) > self.max_audio_bytes:
            raise ASRError('本地 ASR 需要有效 WAV，大小不超过 3 MiB')
        try:
            if not self.endpoint:
                return await asyncio.to_thread((await self._local()).transcribe, audio.data)
            async with self._client().post(self.endpoint + '/v1/transcribe', data=audio.data,
                                          headers={'Content-Type': 'audio/wav'}) as response:
                response.raise_for_status()
                result = await response.json()
                if not isinstance(result.get('text'), str):
                    raise ASRError('本地 ASR 返回无效文本')
                return result['text']
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError, wave.Error) as error:
            raise ASRError('本地 ASR 识别失败') from error

    async def close(self):
        if self._http is not None:
            await self._http.close()
            self._http = None
        self._engine = None
