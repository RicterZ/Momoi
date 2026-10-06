import asyncio
from contextlib import aclosing

from momoi.qq_call.speech import bubble_pcm


def test_prefetch_overlaps_first_bubble_and_preserves_order():
    async def scenario():
        first_release = asyncio.Event()
        next_started = asyncio.Event()
        calls = []

        class Provider:
            async def stream_pcm(self, text):
                calls.append(text)
                if text == 'first':
                    yield b'a'
                    await first_release.wait()
                    yield b'b'
                else:
                    next_started.set()
                    yield b'c'

        async with aclosing(bubble_pcm(Provider(), 'first\n\nsecond')) as stream:
            assert await anext(stream) == b'a'
            await asyncio.wait_for(next_started.wait(), 1)
            assert not first_release.is_set()
            first_release.set()
            assert b''.join([chunk async for chunk in stream]) == b'bc'
        assert calls == ['first', 'second']

    asyncio.run(scenario())


def test_close_cancels_current_and_prefetched_synthesis():
    async def scenario():
        started = [asyncio.Event(), asyncio.Event()]
        closed = [asyncio.Event(), asyncio.Event()]

        class Provider:
            async def stream_pcm(self, text):
                index = int(text)
                started[index].set()
                try:
                    yield bytes([index])
                    await asyncio.Event().wait()
                finally:
                    closed[index].set()

        stream = bubble_pcm(Provider(), '0\n\n1\n\n2')
        assert await anext(stream) == b'\x00'
        await asyncio.wait_for(started[1].wait(), 1)
        await stream.aclose()
        assert all(event.is_set() for event in closed)

    asyncio.run(scenario())


def test_prefetch_failure_is_raised_in_playback_order():
    async def scenario():
        class Provider:
            async def stream_pcm(self, text):
                if text == 'second':
                    raise ValueError('synthesis failed')
                yield b'a'

        async with aclosing(bubble_pcm(Provider(), 'first\n\nsecond')) as stream:
            assert await anext(stream) == b'a'
            try:
                await anext(stream)
            except ValueError as error:
                assert str(error) == 'synthesis failed'
            else:
                assert False, 'Must surface synthesis failure'

    asyncio.run(scenario())
