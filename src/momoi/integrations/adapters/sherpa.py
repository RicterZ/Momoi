"""In-process local CPU speech recognition."""
import asyncio
import io
import os
import sys
from pathlib import Path
import threading
import wave

import numpy as np

from ..contracts.asr import ASRError, ASRProvider, AudioInput


class SherpaEngine:
    def __init__(self, model_path, num_threads=2, trailing_silence=0.8):
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


class SherpaASRProvider(ASRProvider):
    engine = 'zipformer-zh-int8-2025-06-30'

    def __init__(self, *, model_path='', num_threads=2, trailing_silence=0.8):
        install = os.environ.get('MOMOI_INSTALL_DIR')
        if not model_path:
            model_path = str(Path(install).resolve() / 'models' / 'asr') if install else (
                os.environ.get('MOMOI_ASR_MODEL_PATH') or str(Path('models/asr').resolve()))
        from ..validation import number
        number({'num_threads': num_threads}, 'num_threads', 2, integer=True)
        number({'trailing_silence': trailing_silence}, 'trailing_silence', 0.8)
        if num_threads > 16 or trailing_silence > 5:
            raise ValueError('本地 ASR 线程数不能超过 16，断句静音不能超过 5 秒')
        self.model_path, self.num_threads = model_path, num_threads
        self.trailing_silence = trailing_silence
        self._engine = None
        self._dll_directory = None
        self._load_lock = asyncio.Lock()

    async def _local(self):
        async with self._load_lock:
            if self._engine is None:
                try:
                    root = os.environ.get('MOMOI_INSTALL_DIR')
                    if root:
                        libraries = Path(root).resolve() / 'runtime' / 'asr' / 'site-packages'
                        if libraries.is_dir() and str(libraries) not in sys.path:
                            sys.path.insert(0, str(libraries))
                        dlls = libraries / "sherpa_onnx" / "lib"
                        if os.name == "nt" and dlls.is_dir() and self._dll_directory is None:
                            self._dll_directory = os.add_dll_directory(str(dlls))
                    self._engine = await asyncio.to_thread(SherpaEngine, self.model_path,
                                                          self.num_threads, self.trailing_silence)
                except (ImportError, ValueError, RuntimeError, OSError) as error:
                    raise ASRError('本地 ASR 加载失败，请安装本地 ASR 组件并检查模型目录') from error
        return self._engine

    async def create_stream(self):
        engine = await self._local()
        return LocalStream(engine, await asyncio.to_thread(engine.create_stream))

    async def transcribe(self, audio: AudioInput):
        if audio.format != 'wav' or not audio.data or len(audio.data) > self.max_audio_bytes:
            raise ASRError('本地 ASR 需要有效 WAV，大小不超过 3 MiB')
        try:
            return await asyncio.to_thread((await self._local()).transcribe, audio.data)
        except (ValueError, OSError, wave.Error) as error:
            raise ASRError('本地 ASR 识别失败') from error

    async def close(self):
        self._engine = None
