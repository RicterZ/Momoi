"""Run inside the ASR container; file speed plus paced streaming latency."""
import argparse
import json
from pathlib import Path
import platform
import time
import wave

from momoi.integrations.adapters.sherpa import SherpaEngine


def benchmark(model, files, threads, paced, trailing_silence=1.2):
    started = time.perf_counter()
    engine = SherpaEngine(model, threads, trailing_silence)
    print(json.dumps({'platform': platform.platform(), 'machine': platform.machine(),
                      'threads': threads, 'trailing_silence': trailing_silence, 'load_seconds': time.perf_counter() - started}), flush=True)
    for path in files:
        with wave.open(str(path)) as wav:
            rate = wav.getframerate()
            pcm = wav.readframes(wav.getnframes())
        duration = len(pcm) / (2 * rate)
        for repeat in range(3):
            started = time.perf_counter()
            text = engine.transcribe(path.read_bytes())
            elapsed = time.perf_counter() - started
            print(json.dumps({'file': path.name, 'repeat': repeat, 'audio_seconds': duration,
                              'elapsed_seconds': elapsed, 'rtf': elapsed / duration,
                              'text': text}, ensure_ascii=False), flush=True)
        if paced and rate == 16000:
            stream = engine.create_stream()
            first = None
            started = time.perf_counter()
            finals = []
            # Feed speech plus real-time trailing silence. Endpoints must be measured
            # against the audio clock, not against unpaced file decoding throughput.
            source = pcm + bytes(16000 * 2 * 3)
            for offset in range(0, len(source), 640):
                target = (offset + 640) / 32000
                time.sleep(max(0, started + target - time.perf_counter()))
                result = engine.feed(stream, source[offset:offset + 640])
                elapsed = time.perf_counter() - started
                if result['text'] and first is None:
                    first = elapsed
                if result['final'] and result['text']:
                    finals.append({'text': result['text'], 'first_partial_seconds': first,
                                   'final_seconds': elapsed, 'file_end_to_final_seconds': elapsed - duration})
            print(json.dumps({'file': path.name, 'streaming': finals,
                              'joined_text': ''.join(item['text'] for item in finals)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--model', default='/models')
    p.add_argument('--threads', type=int, default=2)
    p.add_argument('--paced', action='store_true')
    p.add_argument('--trailing-silence', type=float, default=1.2)
    p.add_argument('files', type=Path, nargs='+')
    args = p.parse_args()
    benchmark(args.model, args.files, args.threads, args.paced, args.trailing_silence)
