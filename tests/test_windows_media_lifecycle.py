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
    import sys
    available = {0: ['speaker'], 1: ['mic']}
    monkeypatch.setitem(sys.modules, 'virtual_audio', SimpleNamespace(
        active_endpoint_ids=lambda flow: available[flow]))
    audio = DeferredWindowsAudio.__new__(DeferredWindowsAudio)
    audio.backend = SimpleNamespace(ready=True, device_selection={
        'input_device': {'id': 'mic'}, 'output_device': {'id': 'speaker'},
        'injection_device': {'id': 'speaker'}})
    assert audio.endpoints_available()
    available[0] = []
    assert not audio.endpoints_available()
    available[0] = ['speaker']
    assert audio.endpoints_available()
