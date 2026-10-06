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
    class ConnectedSocket:
        closed = False
        def __bool__(self):
            return False  # aiohttp 3.8 StreamResponse is an empty mapping.
    broker.ws = ConnectedSocket()
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


def test_status_reaches_connected_socket_even_when_socket_is_falsey():
    async def scenario():
        class Socket:
            closed = False
            def __bool__(self):
                return False
            async def send_json(self, value):
                self.received = value
        broker = MediaBroker('x' * 32)
        socket = Socket()
        broker.ws = socket
        await broker.send_status()
        assert socket.received['type'] == 'status'
    asyncio.run(scenario())


def test_empty_recognition_never_interrupts_and_valid_text_interrupts_once():
    async def scenario():
        recognized = asyncio.Event()
        class ASR:
            async def transcribe(self, audio):
                if audio.data == b'noise':
                    recognized.set()
                    return '  '
                return 'synthetic reply'
        interruptions, stops, events = [], [], []
        channel = QQCallChannel(SimpleNamespace(), '123', ASR(), tts_enabled=True,
                                interrupt=interruptions.append)
        channel.session_id = 'first'
        channel.status = {'phase': 'connected'}
        async def stop(context):
            stops.append(dict(context))
        channel.stop_playback = stop
        arrived = asyncio.Event()
        async def receive(event):
            events.append(event)
            arrived.set()
        original = dict(channel.routing_context())
        worker = asyncio.create_task(channel.transcribe(receive))
        try:
            await channel.queue.put((original, b'noise'))
            await asyncio.wait_for(recognized.wait(), 1)
            await asyncio.sleep(0)
            assert interruptions == stops == events == []
            assert channel.context_valid(original)
            await channel.queue.put((original, b'speech'))
            await asyncio.wait_for(arrived.wait(), 1)
            assert interruptions == ['owner_speech']
            assert len(stops) == 1 and len(events) == 1
            assert channel.context_valid(events[0].delivery_context)
            assert events[0].delivery_context["recognized_at"] > 0
            assert not channel.context_valid(original)
            # A second queued segment from the same call must survive the first
            # segment's generation change, rather than silently drop the message.
            arrived.clear()
            await channel.queue.put((original, b'speech'))
            await asyncio.wait_for(arrived.wait(), 1)
            assert len(events) == 2 and channel.generation == 2
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(scenario())


def test_hangup_keeps_connection_armed_for_next_call(monkeypatch):
    async def scenario():
        reader = asyncio.StreamReader()
        class Process:
            returncode = None
            stdout = reader
            def terminate(self):
                self.returncode = 0
                reader.feed_eof()
            async def wait(self):
                return self.returncode
        async def spawn(*args, **kwargs):
            return Process()
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        broker = MediaBroker('x' * 32)
        broker.ws = SimpleNamespace(closed=False)
        broker.armed = True
        broker.session_id = 'first'
        broker.status = {'phase': 'connected'}
        broker.capture_task = asyncio.create_task(broker.capture_audio('first'))
        await asyncio.sleep(0)
        assert broker.capture is not None
        await broker.invalidate()
        assert broker.armed
        assert broker.session_id == '' and broker.capture is None
        broker.session_id = 'second'
        assert broker.valid('second', broker.generation)
        assert not broker.valid('first', 0)
    asyncio.run(scenario())


def test_capture_failure_closes_socket_instead_of_leaving_disabled_connection(monkeypatch):
    async def scenario():
        class Socket:
            closed = False
            async def close(self, **kwargs):
                self.closed = True
        async def spawn(*args, **kwargs):
            raise OSError('Synthetic audio device failure')
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        broker = MediaBroker('x' * 32)
        broker.ws = Socket()
        broker.armed = True
        broker.session_id = 'first'
        await broker.capture_audio('first')
        assert broker.ws.closed
        assert broker.capture is None
    asyncio.run(scenario())


def test_speech_detection_only_queues_audio_until_recognition():
    async def scenario():
        from aiohttp import WSMessage, WSMsgType
        class Socket:
            def __aiter__(self):
                async def messages():
                    for frame in [VOICE] * 8 + [SILENCE] * 35:
                        yield WSMessage(WSMsgType.BINARY, frame, '')
                return messages()
        interruptions = []
        channel = QQCallChannel(SimpleNamespace(), '123', object(), tts_enabled=True,
                                interrupt=interruptions.append)
        channel.session_id = 'first'
        channel.status = {'phase': 'connected'}
        channel.ws = Socket()
        await channel.receive_audio(None)
        assert channel.queue.qsize() == 1
        assert channel.generation == 0
        assert interruptions == []
        assert channel.pending_stop is None
    asyncio.run(scenario())
