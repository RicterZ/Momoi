"""Unavailable audio must keep the control endpoint alive and recover later."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from momoi.qq_call.broker import MediaBroker
from momoi.qq_call.windows_audio import DeferredWindowsAudio


def test_virtual_audio_failure_restores_before_retry():
    async def scenario():
        events = []
        class Backend:
            ready = False
            def __init__(self, runtime, *, device_ids=None):
                self.runtime = runtime
            def __enter__(self):
                events.append('enter')
            async def prepare(self, *args):
                raise RuntimeError('No virtual endpoint')
            def __exit__(self, *args):
                events.append('restore')
        audio = DeferredWindowsAudio.__new__(DeferredWindowsAudio)
        audio.factory, audio.runtime, audio.backend = Backend, Path('.'), None
        audio.device_ids = {}
        with pytest.raises(RuntimeError, match='virtual endpoint'):
            await audio.prepare(None, '', '')
        assert events == ['enter', 'restore']
        assert not audio.ready
        assert audio.backend is None
        audio.close()
        assert events == ['enter', 'restore']
    asyncio.run(scenario())


def test_media_starts_before_audio_ready_and_retries():
    async def scenario():
        class Audio:
            ready = False
            attempts = 0
            capture_command = ('capture',)
            playback_command = ('playback',)
            closed = False
            async def prepare(self, *args):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError('No virtual endpoint')
                self.ready = True
            def close(self):
                self.closed = True
        audio = Audio()
        broker = MediaBroker('a' * 64, audio=audio)
        async def dependencies():
            return {'bridge': True, 'av_host': True, 'audio': audio.ready}, {}
        broker.dependencies = dependencies
        await broker.startup(None)
        try:
            for _ in range(100):
                if audio.attempts:
                    break
                await asyncio.sleep(.01)
            assert not broker.poll_task.done()
            assert not broker.status['ready']
            assert 'No virtual endpoint' in broker.status['error']
            broker.audio_retry_at = 0
            for _ in range(150):
                if broker.status['ready']:
                    break
                await asyncio.sleep(.01)
            assert audio.attempts == 2
            assert broker.status['ready']
            assert not broker.status['auto_answer_ready']
            assert broker.capture_command == ('capture',)
        finally:
            await broker.cleanup(None)
        assert audio.closed
    asyncio.run(scenario())


def test_endpoint_loss_detected_and_returning_devices_rechecked(monkeypatch):
    from momoi.qq_call import windows_audio
    available = {"0": ['speaker'], "1": ['mic']}
    async def probe(*args):
        return available
    monkeypatch.setattr(windows_audio, '_read_audio_probe', probe)
    audio = DeferredWindowsAudio.__new__(DeferredWindowsAudio)
    audio.bridge = Path('.')
    audio.backend = SimpleNamespace(ready=True, device_selection={
        'input_device': {'id': 'mic'}, 'output_device': {'id': 'speaker'},
        'injection_device': {'id': 'speaker'}})
    async def scenario():
        assert await audio.check_endpoints()
        available["0"] = []
        assert not await audio.check_endpoints()
        available["0"] = ['speaker']
        assert await audio.check_endpoints()
    asyncio.run(scenario())


def test_device_enumeration_timeout_kills_probe(monkeypatch):
    from momoi.qq_call import windows_audio
    async def scenario():
        class Probe:
            returncode = None
            killed = False
            async def communicate(self):
                raise asyncio.TimeoutError()
            def kill(self):
                self.killed = True
            async def wait(self):
                self.returncode = -1
        probe = Probe()
        async def launch(*args, **kwargs):
            assert '-I' in args
            assert kwargs['stderr'] == asyncio.subprocess.PIPE
            return probe
        monkeypatch.setattr(windows_audio.sys, 'platform', 'win32')
        monkeypatch.setattr(windows_audio.subprocess, 'CREATE_NO_WINDOW', 0x08000000, raising=False)
        monkeypatch.setattr(windows_audio.asyncio, 'create_subprocess_exec', launch)
        with pytest.raises(RuntimeError, match='响应超时'):
            await windows_audio.read_device_catalog(Path('.'))
        assert probe.killed
        assert probe.returncode == -1
    asyncio.run(scenario())


def test_av_host_restart_discards_previous_device_selection():
    async def scenario():
        class Audio:
            ready = True
            closed = 0
            prepared = 0
            capture_command = ('new-capture',)
            playback_command = ('new-playback',)
            def close(self):
                self.ready = False
                self.closed += 1
            async def prepare(self, *args):
                self.ready = True
                self.prepared += 1
        audio = Audio()
        broker = MediaBroker('a' * 64, audio=audio)
        host_up = False
        async def dependencies():
            return {'bridge': host_up, 'av_host': host_up, 'audio': audio.ready}, {}
        broker.dependencies = dependencies
        await broker.startup(None)
        try:
            for _ in range(100):
                if audio.closed:
                    break
                await asyncio.sleep(.01)
            assert audio.closed == 1
            assert not audio.ready
            host_up = True
            for _ in range(150):
                if audio.prepared:
                    break
                await asyncio.sleep(.01)
            assert audio.prepared == 1
            assert broker.capture_command == ('new-capture',)
            assert broker.playback_command == ('new-playback',)
        finally:
            await broker.cleanup(None)
    asyncio.run(scenario())


def test_device_probe_reports_windows_failure(monkeypatch, caplog):
    from momoi.qq_call import windows_audio
    async def scenario():
        class Probe:
            returncode = 1
            async def communicate(self):
                return b'', b'Traceback:\nOSError: Audio policy HRESULT 0x80070490\n'
        async def launch(*args, **kwargs):
            return Probe()
        monkeypatch.setattr(windows_audio.sys, 'platform', 'win32')
        monkeypatch.setattr(windows_audio.subprocess, 'CREATE_NO_WINDOW', 0x08000000, raising=False)
        monkeypatch.setattr(windows_audio.asyncio, 'create_subprocess_exec', launch)
        with pytest.raises(RuntimeError, match='0x80070490'):
            await windows_audio.read_device_catalog(Path('.'))
        assert 'qq_call_device_probe_failed' in caplog.text
    asyncio.run(scenario())
