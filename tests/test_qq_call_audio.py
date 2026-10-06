import asyncio
import struct
import wave
import io
from types import SimpleNamespace

import pytest

from momoi.qq_call.audio import Segmenter, wav_bytes
from momoi.qq_call.broker import MediaBroker
from momoi.qq_call.channel import QQCallChannel
from momoi.channel import SendRejected

SILENCE = bytes(640)
VOICE = struct.pack('<320h', *([3000, -3000] * 160))


def test_segment_retains_preroll_and_finishes_after_silence():
    vad = Segmenter()
    for _ in range(10):
        assert vad.feed(SILENCE) == (False, None)
    starts = [vad.feed(VOICE)[0] for _ in range(8)]
    assert starts.count(True) == 1
    result = None
    for _ in range(35):
        _, result = vad.feed(SILENCE)
    assert result and VOICE in result
    with wave.open(io.BytesIO(wav_bytes(result))) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        assert wav.readframes(wav.getnframes()) == result


def test_segment_bounds_continuous_speech_and_ignores_partial_frames():
    vad = Segmenter()
    assert vad.feed(b'bad') == (False, None)
    results = [vad.feed(VOICE)[1] for _ in range(750)]
    complete = [pcm for pcm in results if pcm]
    assert len(complete) == 1
    assert len(complete[0]) <= 750 * 640


def test_old_call_context_cannot_reach_playback():
    async def scenario():
        channel = QQCallChannel(SimpleNamespace(), '123', object(), tts_enabled=True)
        channel.session_id = 'first'
        channel.status = {'phase': 'connected'}
        context = channel.routing_context()
        assert channel.context_valid(context)
        channel.generation += 1
        assert not channel.context_valid(context)
        with pytest.raises(SendRejected):
            await channel.send_call_voice(None, context, 'reply')
        fresh = channel.routing_context()
        await channel.end_session()
        channel.session_id = 'second'
        assert not channel.context_valid(fresh)
    asyncio.run(scenario())

def test_asr_result_is_discarded_when_owner_interrupts():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class ASR:
            async def transcribe(self, audio):
                entered.set()
                await release.wait()
                return 'synthetic utterance'
        channel = QQCallChannel(SimpleNamespace(), '123', ASR(), tts_enabled=True)
        channel.session_id = 'first'
        channel.status = {'phase': 'connected'}
        await channel.queue.put((channel.routing_context(), wav_bytes(VOICE)))
        events = []
        async def receive(event):
            events.append(event)
        task = asyncio.create_task(channel.transcribe(receive))
        await entered.wait()
        channel.generation += 1
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert events == []
    asyncio.run(scenario())

def test_broker_requires_live_matching_session_and_generation():
    broker = MediaBroker('x' * 32)
    broker.ws = SimpleNamespace(closed=False)
    broker.armed = True
    broker.status = {'phase': 'connected'}
    broker.session_id = 'first'
    assert broker.valid('first', 0)
    assert not broker.valid('other', 0)
    assert not broker.valid('first', 1)
    broker.armed = False
    assert not broker.valid('first', 0)


def test_bridge_authentication_and_single_client_readiness():
    async def scenario():
        from aiohttp import ClientSession, web
        broker = MediaBroker('x' * 32)
        async def dependencies():
            return {'bridge': True, 'av_host': True, 'audio': True}, {'phase': 'idle'}
        async def native(*args, **kwargs):
            return {}
        broker.dependencies = dependencies
        broker.native = native
        runner = web.AppRunner(broker.app())
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        try:
            async with ClientSession() as client:
                async with client.get(base + '/v1/status') as response:
                    assert response.status == 401
                headers = {'Authorization': 'Bearer ' + 'x' * 32}
                async with client.get(base + '/v1/status', headers=headers) as response:
                    assert response.status == 200
                    assert (await response.json())['protocol_version'] == 1
                ws = await client.ws_connect(base + '/v1/audio', headers=headers)
                await ws.send_json({'type': 'ready', 'owner_qq': '123', 'ready': True})
                # Receive the next polling status, proving the readiness frame was processed.
                await asyncio.wait_for(ws.receive_json(), 3)
                assert broker.owner == '123' and broker.armed
                async with client.get(base + '/v1/audio', headers=headers) as response:
                    assert response.status == 409
                await ws.close()
        finally:
            await runner.cleanup()
        assert not broker.armed and not broker.session_id
    asyncio.run(scenario())
