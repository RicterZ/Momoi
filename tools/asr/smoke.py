"""Container-local HTTP/WebSocket checks with real model audio."""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import time
import wave

import aiohttp
from momoi.integrations.adapters.sherpa import SherpaASRProvider
from momoi.integrations.contracts.asr import AudioInput


async def run(path, endpoint, trailing_silence):
    provider = SherpaASRProvider(endpoint=endpoint, trailing_silence=trailing_silence)
    try:
        started = time.perf_counter()
        text = await provider.transcribe(AudioInput(path.read_bytes(), 'wav'))
        print(json.dumps({'http_text': text, 'http_elapsed': time.perf_counter()-started}, ensure_ascii=False), flush=True)
        # A disconnected partial stream must not contaminate the next connection.
        interrupted = await provider.create_stream()
        await interrupted.feed(bytes(640))
        await interrupted.close()
        with wave.open(str(path)) as wav:
            pcm = wav.readframes(wav.getnframes())
        stream = await provider.create_stream()
        timings, results = [], []
        started = time.perf_counter()
        for offset in range(0, len(pcm) + 96000, 640):
            await asyncio.sleep(max(0, started+(offset+640)/32000-time.perf_counter()))
            frame = pcm[offset:offset+640] if offset < len(pcm) else bytes(640)
            if len(frame) < 640:
                frame += bytes(640-len(frame))
            before = time.perf_counter()
            result = await stream.feed(frame)
            timings.append((time.perf_counter()-before)*1000)
            if result['final'] and result['text']:
                results.append(result['text'])
        await stream.close()
        assert ''.join(results) == text, (results, text)
        print(json.dumps({'websocket_text': ''.join(results), 'frames': len(timings),
                          'feed_ms_p50': statistics.median(timings),
                          'feed_ms_p95': sorted(timings)[int(len(timings)*.95)],
                          'feed_ms_max': max(timings), 'session_isolation': True}, ensure_ascii=False), flush=True)
        async with aiohttp.ClientSession() as http:
            async with http.post(endpoint.rstrip('/') + '/v1/transcribe', data=b'not-wav') as response:
                assert response.status == 400, response.status
    finally:
        await provider.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8003")
    parser.add_argument("--trailing-silence", type=float, default=0.8)
    args = parser.parse_args()
    asyncio.run(run(args.audio, args.endpoint, args.trailing_silence))
