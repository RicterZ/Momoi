"""Ordered bubble audio with bounded, cancellable TTS prefetch."""

import asyncio
from time import monotonic
from contextlib import aclosing


async def bubble_pcm(provider, text: str, *, pause_seconds: float = 0, paced: bool = False):
    bubbles = [part.strip() for part in text.split("\n\n") if part.strip()]
    queues = {}
    tasks = {}
    finished = object()
    playback_end = None

    async def synthesize(index, queue):
        try:
            async with aclosing(provider.stream_pcm(bubbles[index])) as stream:
                async for chunk in stream:
                    await queue.put(chunk)
        except Exception as error:
            await queue.put(error)
        else:
            await queue.put(finished)

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
                    raise item
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
                yield item
                if paced:
                    await asyncio.sleep(max(0, playback_end - monotonic()))
            await tasks.pop(index)
            del queues[index]
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
