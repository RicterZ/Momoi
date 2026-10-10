import asyncio
from types import SimpleNamespace

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from momoi.integrations.adapters.fish import FishAudioTTSProvider
from momoi.channel.napcat.voice_call.broker import MediaBroker


def test_fish_yields_audio_before_response_finishes():
    async def scenario():
        release = asyncio.Event()
        async def tts(request):
            body = await request.json()
            assert body['format'] == 'pcm' and body['sample_rate'] == 24000
            response = web.StreamResponse(headers={'Content-Type': 'audio/pcm'})
            await response.prepare(request)
            await response.write(b'\x01\x00' * 480)
            await release.wait()
            await response.write(b'\x02\x00' * 480)
            return response
        app = web.Application()
        app.router.add_post('/v1/tts', tts)
        async with TestServer(app) as server:
            provider = FishAudioTTSProvider(api_key='synthetic', base_url=str(server.make_url('')).rstrip('/'))
            stream = provider.stream_pcm('synthetic speech')
            try:
                first = await asyncio.wait_for(anext(stream), 2)
                assert first == b'\x01\x00' * 480
                assert not release.is_set()
                release.set()
                rest = b''.join([chunk async for chunk in stream])
                assert rest == b'\x02\x00' * 480
            finally:
                release.set()
                await stream.aclose()
    asyncio.run(scenario())


def test_broker_plays_before_upload_finishes_and_deduplicates(monkeypatch):
    from momoi.channel.napcat.voice_call import broker as broker_module
    async def observe(*args):
        return 12
    monkeypatch.setattr(broker_module, 'observe_playback_audio', observe)
    monkeypatch.setattr(broker_module.sys, 'platform', 'linux')
    async def scenario():
        wrote = asyncio.Event()
        release = asyncio.Event()
        processes = []
        class Process:
            returncode = None
            def __init__(self):
                self.stdin = self
                self.audio = bytearray()
            def write(self, data):
                self.audio.extend(data)
                wrote.set()
            async def drain(self):
                pass
            def close(self):
                self.returncode = 0
            async def wait(self):
                return self.returncode
            def terminate(self):
                self.returncode = -15
        async def spawn(*args, **kwargs):
            process = Process()
            processes.append(process)
            return process
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        broker = MediaBroker('x' * 32)
        broker.ws = SimpleNamespace(closed=False)
        broker.armed = True
        broker.session_id = 'synthetic'
        broker.status = {'phase': 'connected'}
        app = web.Application()
        app.router.add_post('/stream', broker.play_stream)
        headers = {'X-Call-Session': 'synthetic', 'X-Call-Generation': '0', 'X-Call-Utterance': 'one'}
        async def upload():
            yield bytes(960)
            await release.wait()
            yield bytes(960)
        async with TestServer(app) as server, ClientSession() as client:
            task = asyncio.create_task(client.post(server.make_url('/stream'), data=upload(), headers=headers))
            try:
                await asyncio.wait_for(wrote.wait(), 2)
                assert not release.is_set()
                release.set()
                response = await task
                result = await response.json()
                assert result['ok'] and result['played_ms'] == 40
                assert result['first_frame_ms'] is not None
                assert result['virtual_mic_signal_ms'] == 12
                assert result['remote_audible_at'] is None
                async with client.post(server.make_url('/stream'), data=bytes(960), headers=headers) as response:
                    assert await response.json() == result
                assert len(processes) == 1
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_playback_observer_ignores_silence_and_cleans_up(monkeypatch, capsys):
    import json
    import time
    from momoi.channel.napcat.voice_call.broker import observe_playback_audio

    async def scenario():
        class Process:
            returncode = None
            def __init__(self):
                self.stdout = asyncio.StreamReader()
            def terminate(self):
                self.returncode = -15
            async def wait(self):
                return self.returncode
        processes = []
        async def spawn(*args, **kwargs):
            process = Process()
            processes.append(process)
            return process
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        task = asyncio.create_task(observe_playback_audio('session', 'utterance', time.monotonic()))
        await asyncio.sleep(0)
        processes[0].stdout.feed_data(bytes(960))
        await asyncio.sleep(0)
        assert not task.done()
        processes[0].stdout.feed_data(b'\x00\x01' * 480)
        assert await task is not None
        assert processes[0].returncode == -15
        event = json.loads(capsys.readouterr().out)
        assert event['event'] == 'qq_call_virtual_mic_signal'
        assert event['utterance_id'] == 'utterance'
        assert event['remote_audible_at'] is None
        task = asyncio.create_task(observe_playback_audio('session', 'cancelled', time.monotonic()))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert processes[1].returncode == -15
        assert not capsys.readouterr().out
    asyncio.run(scenario())


def test_hangup_cancels_upstream_stream_while_waiting_for_audio():
    async def scenario():
        from momoi.channel.napcat.voice_call.channel import QQCallChannel
        from momoi.channel import SendInterrupted
        entered, closed = asyncio.Event(), asyncio.Event()
        class Provider:
            async def stream_pcm(self, text):
                try:
                    entered.set()
                    await asyncio.Event().wait()
                    yield bytes(960)
                finally:
                    closed.set()
        async def playback(request):
            await request.read()
            return web.json_response({'ok': True})
        app = web.Application()
        app.router.add_post('/v1/playback/stream', playback)
        async with TestServer(app) as server, ClientSession() as http:
            channel = QQCallChannel(SimpleNamespace(bridge_url=str(server.make_url('')).rstrip('/'),
                bridge_token='x' * 32), '123', object(), tts_enabled=True)
            channel.http = http
            channel.session_id = 'first'
            channel.status = {'phase': 'connected'}
            task = asyncio.create_task(channel.send_call_stream(Provider(), 'synthetic', channel.routing_context(), 'one'))
            await asyncio.wait_for(entered.wait(), 2)
            await channel.end_session()
            try:
                await asyncio.wait_for(task, 2)
                assert False, 'Stale stream must be rejected'
            except SendInterrupted:
                pass
            await asyncio.wait_for(closed.wait(), 2)
    asyncio.run(scenario())


def test_channel_sends_bubbles_as_one_ordered_playback_stream():
    async def scenario():
        from momoi.channel.napcat.voice_call.channel import QQCallChannel
        calls = []
        uploads = []

        class Provider:
            async def stream_pcm(self, text):
                calls.append(text)
                yield (b'\x01\x00' if text == 'first' else b'\x02\x00') * 480

        async def playback(request):
            uploads.append(await request.read())
            return web.json_response({'ok': True, 'state': 'played', 'played_ms': 40})

        app = web.Application()
        app.router.add_post('/v1/playback/stream', playback)
        async with TestServer(app) as server, ClientSession() as http:
            channel = QQCallChannel(SimpleNamespace(bridge_url=str(server.make_url('')).rstrip('/'),
                bridge_token='x' * 32), '123', object(), tts_enabled=True)
            channel.http = http
            channel.session_id = 'synthetic'
            channel.status = {'phase': 'connected'}
            await channel.send_call_stream(Provider(), 'first\n\nsecond', channel.routing_context(), 'one')
        assert calls == ['first', 'second']
        assert len(uploads) == 1
        assert uploads[0].startswith(b'\x01\x00' * 480)
        assert uploads[0].endswith(b'\x02\x00' * 480)
        silence = uploads[0][960:-960]
        assert 22000 <= len(silence) <= 24000 and not any(silence)

    asyncio.run(scenario())


def test_channel_preserves_synthesis_error_in_streaming_failure():
    async def scenario():
        from momoi.channel.napcat.voice_call.channel import QQCallChannel
        from momoi.channel import SendRejected
        from momoi.integrations.contracts.tts import TTSError

        class Provider:
            async def stream_pcm(self, text):
                raise TTSError('Synthetic TTS returned HTTP 404')
                yield bytes(960)

        async def playback(request):
            try:
                await request.read()
            except ConnectionResetError:
                pass
            return web.json_response({'ok': False})

        app = web.Application()
        app.router.add_post('/v1/playback/stream', playback)
        async with TestServer(app) as server, ClientSession() as http:
            channel = QQCallChannel(SimpleNamespace(bridge_url=str(server.make_url('')).rstrip('/'),
                bridge_token='x' * 32), '123', object(), tts_enabled=True)
            channel.http = http
            channel.session_id = 'synthetic'
            channel.status = {'phase': 'connected'}
            try:
                await channel.send_call_stream(Provider(), 'synthetic', channel.routing_context(), 'one')
            except SendRejected as error:
                assert 'Synthetic TTS returned HTTP 404' in str(error)
            else:
                assert False, 'Must preserve synthesis error'

    asyncio.run(scenario())


def test_bubble_tts_retries_three_times_and_continues_other_bubbles(monkeypatch):
    from unittest.mock import AsyncMock
    from momoi.channel.napcat.voice_call.speech import bubble_pcm
    from momoi.integrations.contracts.tts import TTSError
    delay = AsyncMock()
    monkeypatch.setattr('momoi.channel.napcat.voice_call.speech.asyncio.sleep', delay)
    attempts = {}
    class Provider:
        async def stream_pcm(self, text):
            attempts[text] = attempts.get(text, 0) + 1
            if text == 'failed' or (text == 'retry' and attempts[text] < 4):
                raise TTSError('synthetic error')
            yield b'\x01\x00'
    async def scenario():
        audio = b''.join([chunk async for chunk in bubble_pcm(Provider(), 'failed\n\nretry\n\nsuccess')])
        assert audio == b'\x01\x00' * 2
        assert attempts == {'failed': 4, 'retry': 4, 'success': 1}
        assert sorted(call.args[0] for call in delay.await_args_list) == [1, 1, 2, 2, 3, 3]
        import pytest
        with pytest.raises(TTSError):
            _ = [chunk async for chunk in bubble_pcm(Provider(), 'failed')]
    asyncio.run(scenario())


def test_bubble_partial_failure_never_replays_audio_and_continues():
    from momoi.channel.napcat.voice_call.speech import bubble_pcm
    from momoi.integrations.contracts.tts import TTSError
    calls = []
    class Provider:
        async def stream_pcm(self, text):
            calls.append(text)
            yield b'\x01\x00'
            if text == 'partial':
                raise TTSError('synthetic partial failure')
    async def scenario():
        chunks = [chunk async for chunk in bubble_pcm(Provider(), 'partial\n\nnext')]
        assert chunks[0] == chunks[-1] == b'\x01\x00'
        assert sum(chunk.count(b'\x01') for chunk in chunks) == 2
        assert calls == ['partial', 'next']
    asyncio.run(scenario())
