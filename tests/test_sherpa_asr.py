import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web, WSMessage, WSMsgType
from aiohttp.test_utils import TestServer

from momoi.integrations.adapters.sherpa import SherpaASRProvider
from momoi.integrations.contracts.asr import ASRError, AudioInput
from momoi.qq_call.channel import QQCallChannel


def test_remote_asr_batch_and_stream_share_provider():
    async def run():
        async def batch(request):
            assert await request.read() == b'wav-data'
            assert request.query['trailing_silence'] == '0.8'
            assert request.query['num_threads'] == '3'
            return web.json_response({'text': '测试'})
        async def stream(request):
            assert request.query['trailing_silence'] == '0.8'
            assert request.query['num_threads'] == '3'
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for message in ws:
                assert message.data == b'\0\0' * 320
                await ws.send_json({'text': '流式测试', 'final': True})
            return ws
        app = web.Application()
        app.router.add_post('/v1/transcribe', batch)
        app.router.add_get('/v1/stream', stream)
        async with TestServer(app) as server:
            provider = SherpaASRProvider(endpoint=str(server.make_url('')).rstrip('/'), num_threads=3)
            try:
                assert await provider.transcribe(AudioInput(b'wav-data', 'wav')) == '测试'
                session = await provider.create_stream()
                assert await session.feed(b'\0\0' * 320) == {'text': '流式测试', 'final': True}
                await session.close()
                with pytest.raises(ASRError):
                    await provider.transcribe(AudioInput(b'data', 'mp3'))
            finally:
                await provider.close()
    asyncio.run(run())


def test_stream_isolated_across_calls_and_only_final_is_delivered():
    async def run():
        sessions = []
        class Stream:
            closed = False
            count = 0
            async def feed(self, pcm):
                self.count += 1
                return {'text': '测试', 'final': self.count == 2}
            async def close(self):
                self.closed = True
        class Provider:
            async def create_stream(self):
                result = Stream()
                sessions.append(result)
                return result
            async def transcribe(self, audio):
                raise AssertionError('Streaming text must not be transcribed twice')
        class Socket:
            def __aiter__(self):
                async def messages():
                    for _ in range(2):
                        yield WSMessage(WSMsgType.BINARY, bytes(640), '')
                return messages()
        channel = QQCallChannel(SimpleNamespace(), '123', Provider(), tts_enabled=True)
        for sid in ('first', 'second'):
            channel.session_id = sid
            channel.status = {'phase': 'connected'}
            channel.ws = Socket()
            await channel.receive_audio(None)
            assert channel.queue.qsize() == 1
            context, text = channel.queue.get_nowait()
            assert context['call_session_id'] == sid and text == '测试'
            await channel.end_session()
            assert sessions[-1].closed and channel.asr_stream is None
        assert len(sessions) == 2
    asyncio.run(run())


def test_bad_local_configuration_does_not_load_models():
    with pytest.raises(ValueError):
        SherpaASRProvider(endpoint='http://localhost:8003', model_path='/models')
    with pytest.raises(ValueError):
        SherpaASRProvider()
    with pytest.raises(ValueError):
        SherpaASRProvider(endpoint='http://localhost:8003', num_threads=17)


def test_dashboard_switches_asr_without_cloud_credentials(tmp_path):
    from momoi.config.manager import ConfigurationManager
    from momoi.config.workspace import bootstrap
    from momoi.integrations.registry import ServiceRegistry
    from momoi.integrations.contracts.asr import ASRProvider
    from momoi.integrations.adapters.tencent import TencentASRProvider
    path = tmp_path / 'config.json'
    bootstrap(path)
    manager = ConfigurationManager(path)
    local = {'adapter': 'sherpa', 'enabled': True,
             'options': {'endpoint': 'http://asr:8003'}}
    snapshot = manager.save_bindings({'asr': local}, manager.revision())
    registry = ServiceRegistry(manager.validate().providers)
    assert isinstance(registry.asr, ASRProvider)
    assert isinstance(registry.asr, SherpaASRProvider)
    assert snapshot['capabilities']['asr']['adapter'] == 'sherpa'
    cloud = {'adapter': 'tencent', 'enabled': True,
             'options': {'secret_id': 'test-id', 'secret_key': 'test-key'}}
    manager.save_bindings({'asr': cloud}, manager.revision())
    assert isinstance(ServiceRegistry(manager.validate().providers).asr, TencentASRProvider)
    manager.save_bindings({'asr': local}, manager.revision())
    assert isinstance(ServiceRegistry(manager.validate().providers).asr, SherpaASRProvider)
    disabled = {**local, 'enabled': False}
    manager.save_bindings({'asr': disabled}, manager.revision())
    assert ServiceRegistry(manager.validate().providers).asr is None


def test_windows_optional_component_defaults_without_loading(monkeypatch, tmp_path):
    monkeypatch.setattr('momoi.integrations.adapters.sherpa.sys.platform', 'win32')
    monkeypatch.setenv('MOMOI_INSTALL_DIR', str(tmp_path))
    provider = SherpaASRProvider()
    assert provider.endpoint == ''
    assert provider.model_path == str(tmp_path / 'models' / 'asr')
    assert provider._engine is None
    remote = SherpaASRProvider(endpoint='http://asr:8003')
    assert remote.endpoint == ''
    assert remote.model_path == str(tmp_path / 'models' / 'asr')
    assert remote._engine is None


def test_linux_remote_endpoint_is_preserved(monkeypatch, tmp_path):
    monkeypatch.setattr('momoi.integrations.adapters.sherpa.sys.platform', 'linux')
    monkeypatch.setenv('MOMOI_INSTALL_DIR', str(tmp_path))
    remote = SherpaASRProvider(endpoint='http://asr:8003')
    assert remote.endpoint == 'http://asr:8003'
    assert remote.model_path == ''
    with pytest.raises(ValueError):
        SherpaASRProvider(endpoint='http://asr:8003', model_path='/models')


def test_optional_component_resolves_relative_install_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('MOMOI_INSTALL_DIR', 'install')
    provider = SherpaASRProvider()
    assert provider.model_path == str(tmp_path / 'install' / 'models' / 'asr')
    assert provider._engine is None


def test_windows_shell_ignores_saved_remote_address_and_custom_model(monkeypatch, tmp_path):
    monkeypatch.setenv('MOMOI_INSTALL_DIR', str(tmp_path))
    monkeypatch.setattr('momoi.integrations.adapters.sherpa.sys.platform', 'win32')
    provider = SherpaASRProvider(endpoint='http://asr:8003', model_path='/old/models')
    assert provider.endpoint == ''
    assert provider.model_path == str(tmp_path / 'models' / 'asr')
    assert provider._engine is None
    from momoi.config.manager import ConfigurationManager
    from momoi.config.workspace import bootstrap
    path = tmp_path / 'config.json'
    bootstrap(path)
    manager = ConfigurationManager(path)
    assert manager.snapshot()['desktop_asr_managed'] is True
    monkeypatch.setattr('momoi.integrations.adapters.sherpa.sys.platform', 'linux')
    assert manager.snapshot()['desktop_asr_managed'] is False
    remote = SherpaASRProvider(endpoint='http://asr:8003')
    assert remote.endpoint == 'http://asr:8003'
    assert remote.model_path == ''
