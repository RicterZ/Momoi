import asyncio
import logging
from types import SimpleNamespace

import pytest

from momoi.channel.onebot.config import QQCallConfig
from momoi.channel.onebot.voice_call.channel import QQCallChannel, audible_end
from momoi.channel.onebot.voice_call.gate import CaptureGate, detect_half_duplex


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def cable(direction, device_id=''):
    return {'id': device_id, 'name': f'CABLE {direction} (VB-Audio Virtual Cable)'}


def test_detects_shared_cable_by_id_pair_and_name_fallback():
    single_cable = {'input_device': cable('Output', '{0.0.1}.{c}'), 'output_device': cable('Input', '{0.0.0}.{c}'),
                    'injection_device': cable('Input', '{0.0.0}.{C}')}
    assert detect_half_duplex(single_cable) == (True, 'shared_virtual_cable')
    steam = {'input_device': {'id': '{0.0.1}.{mic}', 'name': '麦克风 (Steam Streaming Microphone)'},
             'output_device': {'id': '{0.0.0}.{spk}', 'name': '扬声器 (Steam Streaming Speakers)'},
             'injection_device': {'id': '{0.0.0}.{inj}', 'name': '扬声器 (Steam Streaming Microphone)'}}
    assert detect_half_duplex(steam) == (False, 'separate_devices')
    assert detect_half_duplex({'output_device': cable('Input'), 'injection_device': cable('Input')}) == (
        True, 'shared_virtual_cable_name')
    assert detect_half_duplex({'input_device': cable('Output'), 'output_device': cable('Input')}) == (
        True, 'shared_virtual_cable_name')
    assert detect_half_duplex({}, bridge_flag=True)[0]
    assert detect_half_duplex({'input_device': cable('Output'), 'output_device': {
        'name': 'CABLE-A Input (VB-Audio Cable A)'}}) == (False, 'separate_devices')
    assert detect_half_duplex(None) == (False, 'separate_devices')
    assert detect_half_duplex({}, mode='on') == (True, 'forced_on')
    assert detect_half_duplex(single_cable, mode='off') == (False, 'forced_off')


def test_config_validates_half_duplex_options():
    assert QQCallConfig.from_mapping({}).half_duplex == 'auto'
    assert QQCallConfig.from_mapping({}).half_duplex_tail_ms == 400
    assert QQCallConfig.from_mapping({'half_duplex': 'on', 'half_duplex_tail_ms': 0}).half_duplex == 'on'
    for bad in ({'half_duplex': 'yes'}, {'half_duplex_tail_ms': -1}, {'half_duplex_tail_ms': 1.5}):
        with pytest.raises(ValueError):
            QQCallConfig.from_mapping(bad)


def test_single_sentence_opens_after_audible_end_plus_tail(caplog):
    caplog.set_level(logging.INFO, logger='momoi.qq_call')
    clock = Clock()
    gate = CaptureGate(400, clock)
    gate.turn_started('t1')
    assert gate.closed
    clock.now = 102.0
    gate.send_started('u1')
    clock.now = 103.0
    gate.send_finished(audible_end=104.0)
    gate.turn_finished('t1')
    gate.drop(500)
    clock.now = 104.39
    assert not gate.poll() and gate.closed
    clock.now = 104.4
    assert gate.poll() and not gate.closed
    assert not gate.poll()
    events = {r.momoi_event: r.momoi_fields for r in caplog.records if hasattr(r, 'momoi_event')}
    assert events['qq_call_capture_gate_closed'] == {'reason': 'turn_started', 'turn_id': 't1'}
    assert events['qq_call_capture_gate_opened']['tail_ms'] == 400
    assert events['qq_call_capture_gate_opened']['dropped_frames'] == 1


def test_multi_sentence_stays_closed_between_sentences():
    clock = Clock()
    gate = CaptureGate(400, clock)
    gate.turn_started('t')
    gate.send_started('a')
    gate.send_finished(audible_end=101.0)
    clock.now = 102.0
    assert not gate.poll()
    gate.send_started('b')
    gate.send_finished(audible_end=105.0)
    clock.now = 106.0
    assert not gate.poll()
    gate.turn_finished('t')
    clock.now = 105.3
    assert not gate.poll()
    clock.now = 105.4
    assert gate.poll()


def test_send_without_turn_closes_and_failed_playback_waits_tail_from_failure():
    clock = Clock()
    gate = CaptureGate(400, clock)
    gate.send_started('u')
    assert gate.closed and gate.turn_id is None
    clock.now = 101.0
    gate.send_finished(audible_end=None)
    clock.now = 101.39
    assert not gate.poll()
    clock.now = 101.4
    assert gate.poll()


def test_cancelled_turn_without_audio_opens_tail_after_cancel():
    clock = Clock()
    gate = CaptureGate(400, clock)
    gate.turn_started('t')
    clock.now = 103.0
    gate.turn_finished('t', cancelled=True)
    clock.now = 103.3
    assert not gate.poll()
    clock.now = 103.4
    assert gate.poll()


def test_cancelled_turn_with_in_flight_send_waits_for_send():
    clock = Clock()
    gate = CaptureGate(400, clock)
    gate.turn_started('t')
    gate.send_started('u')
    gate.turn_finished('t', cancelled=True)
    clock.now = 110.0
    assert not gate.poll()
    gate.send_finished(audible_end=None)
    clock.now = 110.4
    assert gate.poll()


def test_audible_end_uses_first_frame_plus_pcm_when_later_than_response():
    assert audible_end(10.0, {'first_frame_ms': 200, 'played_ms': 3000, 'elapsed_ms': 3100}) == 13.2


def test_audible_end_uses_last_write_when_streamed_tts_stalls():
    assert audible_end(10.0, {'first_frame_ms': 15, 'played_ms': 4702, 'elapsed_ms': 7938,
                              'loopback_latency_ms': 1200}) == pytest.approx(19.138)


class Stream:
    def __init__(self):
        self.closed = False
        self.fed = []

    async def feed(self, pcm):
        self.fed.append(pcm)
        return {'final': False, 'text': ''}

    async def close(self):
        self.closed = True


def make_channel(clock):
    channel = QQCallChannel(SimpleNamespace(half_duplex='auto', half_duplex_tail_ms=400), '1',
                            None, tts_enabled=False)
    channel.gate = CaptureGate(400, clock)
    channel.half_duplex = True
    channel.session_id = 's'
    channel.status = {'phase': 'connected'}
    return channel


def test_gate_drops_frames_and_resets_vad_and_asr_on_open():
    async def scenario():
        clock = Clock()
        channel = make_channel(clock)
        stream = channel.asr_stream = Stream()
        channel.segmenter.frames = [b'x' * 640]
        channel.capture_turn_started('t')
        assert not await channel.admit_frame(b'\x10\x00' * 320)
        assert channel.gate.dropped == 1 and channel.gate.dropped_peak == 16
        channel.capture_turn_finished('t')
        clock.now += .4
        assert await channel.admit_frame(bytes(640))
        assert stream.closed and channel.asr_stream is None
        assert channel.segmenter.frames == []
    asyncio.run(scenario())


def test_half_duplex_ignores_interrupt_and_drops_speech_while_closed(caplog):
    caplog.set_level(logging.INFO, logger='momoi.qq_call')
    async def scenario():
        channel = make_channel(Clock())
        interrupts, events = [], []
        channel.interrupt = interrupts.append
        async def on_event(event):
            events.append(event.text)

        async def say(text):
            channel.queue.put_nowait(({**channel.routing_context()}, text))
            while channel.queue.qsize():
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.01)
        worker = asyncio.create_task(channel.transcribe(on_event))
        channel.capture_turn_started('t')
        await say('就一')
        channel.gate.reset()
        await say('你好')
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        return channel, interrupts, events
    channel, interrupts, events = asyncio.run(scenario())
    assert interrupts == [] and channel.generation == 0
    assert events == ['你好']
    assert 'qq_call_interrupt' not in {getattr(r, 'momoi_event', '') for r in caplog.records}


def test_end_session_clears_gate_state():
    async def scenario():
        channel = make_channel(Clock())
        channel.capture_turn_started('t')
        channel.gate.send_started('u')
        await channel.end_session()
        assert not channel.gate.closed and channel.gate.sends == 0 and not channel.half_duplex
    asyncio.run(scenario())


def test_full_duplex_never_gates():
    async def scenario():
        channel = make_channel(Clock())
        channel.half_duplex = False
        channel.capture_turn_started('t')
        assert not channel.gate.closed
    asyncio.run(scenario())


def test_audible_end_adds_measured_loopback_latency():
    assert audible_end(10.0, {'first_frame_ms': 200, 'played_ms': 3000,
                              'loopback_latency_ms': 1300}) == pytest.approx(14.5)


def test_broker_measures_loopback_latency_from_capture(monkeypatch, capsys):
    import json
    import time
    from momoi.channel.onebot.voice_call.broker import MediaBroker

    async def scenario():
        class Process:
            returncode = None
            def __init__(self):
                self.stdout = asyncio.StreamReader()
            def terminate(self):
                self.returncode = -15
            async def wait(self):
                return self.returncode
        process = Process()
        async def spawn(*args, **kwargs):
            return process
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        sent = []
        broker = MediaBroker('x' * 32, audio=SimpleNamespace(capture_command=('c',), playback_command=('p',)))
        broker.session_id = 's'
        broker.ws = SimpleNamespace(closed=False, send_bytes=lambda frame: asyncio.sleep(0, sent.append(frame)))
        broker.capture_stats = {}
        broker.loopback_probe = time.monotonic() - 1.2
        task = asyncio.create_task(broker.capture_audio('s'))
        process.stdout.feed_data(b'\x58\x02' * 320)
        while len(sent) < 1:
            await asyncio.sleep(0.001)
        assert broker.loopback_probe is not None and broker.loopback_latency is None
        process.stdout.feed_data(b'\x00\x10' * 320)
        while len(sent) < 2:
            await asyncio.sleep(0.001)
        assert broker.loopback_probe is None and 1.2 <= broker.loopback_latency < 2
        broker.session_id = ''
        process.stdout.feed_eof()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(e['event'] == 'qq_call_loopback_latency' and e['latency_ms'] >= 1200 for e in events)


def test_broker_arms_loopback_probe_after_leading_quiet_frames(monkeypatch):
    from aiohttp import ClientSession, web
    from aiohttp.test_utils import TestServer
    from momoi.channel.onebot.voice_call.broker import MediaBroker

    async def scenario():
        class Process:
            returncode = None
            def __init__(self):
                self.stdin = self
                self.stderr = asyncio.StreamReader()
                self.stderr.feed_data(b'event=qq_call_wasapi_opened mode=playback\n')
            def write(self, data):
                pass
            async def drain(self):
                pass
            async def wait(self):
                return self.returncode
            def terminate(self):
                self.returncode = -15
        async def spawn(*args, **kwargs):
            return Process()
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        broker = MediaBroker('x' * 32, audio=SimpleNamespace(capture_command=('c',), playback_command=('p',)))
        broker.ws = SimpleNamespace(closed=False)
        broker.armed = True
        broker.session_id = 's'
        broker.status = {'phase': 'connected'}
        broker.loopback_probe = None
        app = web.Application()
        app.router.add_post('/stream', broker.play_stream)
        headers = {'X-Call-Session': 's', 'X-Call-Generation': '0', 'X-Call-Utterance': 'one'}
        body = b'\x14\x00' * 480 * 3 + b'\x00\x30' * 480
        async with TestServer(app) as server, ClientSession() as client:
            async with client.post(server.make_url('/stream'), data=body, headers=headers) as response:
                assert (await response.json())['ok']
        assert broker.loopback_probe is not None
        await broker.output.close()
    asyncio.run(scenario())


class FakePlayer:
    returncode = None

    def __init__(self, stderr=False):
        self.stdin = self
        self.written = bytearray()
        self.stderr = asyncio.StreamReader() if stderr else None

    def write(self, data):
        self.written.extend(data)

    async def drain(self):
        pass

    async def wait(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15


def test_windows_player_gets_no_silence_until_device_opens(monkeypatch, capsys):
    from momoi.channel.onebot.voice_call.broker import SessionPlayback

    async def scenario():
        spawned = []
        async def spawn(*args, **kwargs):
            assert kwargs['stderr'] == asyncio.subprocess.PIPE
            spawned.append(FakePlayer(stderr=True))
            return spawned[-1]
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        output = SessionPlayback(('p',), ready_marker=b'event=qq_call_wasapi_opened')
        await output.start()
        player = spawned[0]
        await asyncio.sleep(0.1)
        assert not player.written
        player.stderr.feed_data(b'2026 DEBUG event=qq_call_wasapi_opened mode=playback\n')
        await asyncio.sleep(0.1)
        assert player.written
        await output.close()
    asyncio.run(scenario())
    assert 'qq_call_wasapi_opened' in capsys.readouterr().err


def test_linux_player_keeps_immediate_silence_prefill(monkeypatch):
    from momoi.channel.onebot.voice_call.broker import MediaBroker

    async def scenario():
        spawned = []
        async def spawn(*args, **kwargs):
            assert kwargs['stderr'] is None
            spawned.append(FakePlayer())
            return spawned[-1]
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        broker = MediaBroker('x' * 32)
        output = await broker.session_output()
        await asyncio.sleep(0.05)
        assert spawned[0].written
        await output.close()
    asyncio.run(scenario())
