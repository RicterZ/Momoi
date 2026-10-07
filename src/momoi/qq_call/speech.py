"""Ordered bubble audio with bounded, cancellable TTS prefetch."""

import asyncio
import logging
from ..observability.events import log_event
from time import perf_counter as monotonic
from contextlib import aclosing


logger = logging.getLogger(__name__)

async def bubble_pcm(provider, text: str, *, pause_seconds: float = 0, paced: bool = False):
    bubbles = [part.strip() for part in text.split("\n\n") if part.strip()]
    queues = {}
    tasks = {}
    finished = object()
    playback_end = None
    audio_emitted = False
    last_error = None

    async def synthesize(index, queue):
        emitted = False
        for attempt in range(4):
            try:
                async with aclosing(provider.stream_pcm(bubbles[index])) as stream:
                    async for chunk in stream:
                        if chunk:
                            await queue.put(chunk)
                            emitted = True
                await queue.put(finished)
                return
            except Exception as error:
                if emitted or attempt == 3:
                    log_event(logger, logging.WARNING, "qq_call_bubble_tts_failure",
                              bubble_index=index, attempts=attempt + 1,
                              partial_audio=emitted, error_type=type(error).__name__)
                    await queue.put(error)
                    return
                delay = attempt + 1
                log_event(logger, logging.WARNING, "qq_call_bubble_tts_retry",
                          bubble_index=index, attempt=attempt + 1,
                          delay_seconds=delay, error_type=type(error).__name__)
                await asyncio.sleep(delay)

    def start(index):
        if index < len(bubbles) and index not in tasks:
            queue = asyncio.Queue(maxsize=32)
            queues[index] = queue
            tasks[index] = asyncio.create_task(synthesize(index, queue))

    try:
        for index in range(len(bubbles)):
            start(index)
            start(index + 1)
            first_chunk = True
            while True:
                item = await queues[index].get()
                if item is finished:
                    break
                if isinstance(item, Exception):
                    last_error = item
                    break
                if first_chunk and index and playback_end is not None:
                    # PCM duration is the playback clock. Synthesis/network wait
                    # already counts toward the intended inter-bubble pause.
                    remaining = max(0, playback_end + pause_seconds - monotonic())
                    if remaining:
                        silence = bytes(round(remaining * 24000) * 2)
                        yield silence
                        playback_end = max(playback_end, monotonic()) + len(silence) / 48000
                        if paced:
                            await asyncio.sleep(max(0, playback_end - monotonic()))
                first_chunk = False
                now = monotonic()
                playback_end = max(playback_end or now, now) + len(item) / 48000
                audio_emitted = True
                yield item
                if paced:
                    await asyncio.sleep(max(0, playback_end - monotonic()))
            await tasks.pop(index)
            del queues[index]
        if not audio_emitted and last_error is not None:
            raise last_error
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
