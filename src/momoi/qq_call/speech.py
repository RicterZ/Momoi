"""Ordered bubble audio with bounded, cancellable TTS prefetch."""

import asyncio
from contextlib import aclosing


async def bubble_pcm(provider, text: str):
    bubbles = [part.strip() for part in text.split("\n\n") if part.strip()]
    queues = {}
    tasks = {}
    finished = object()

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
            while True:
                item = await queues[index].get()
                if item is finished:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
            await tasks.pop(index)
            del queues[index]
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
