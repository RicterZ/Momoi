import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import WSMessage, WSMsgType

from momoi.integrations.adapters.sherpa import SherpaASRProvider
from momoi.integrations.contracts.asr import ASRError, AudioInput
from momoi.channel.napcat.voice_call.channel import QQCallChannel


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
    with pytest.raises(TypeError):
        SherpaASRProvider(endpoint='http://localhost:8003', model_path='/models')
    with pytest.raises(ValueError):
        SherpaASRProvider(num_threads=17)


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
             'options': {}}
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
    assert provider.model_path == str(tmp_path / 'models' / 'asr')
    assert provider._engine is None


def test_optional_component_resolves_relative_install_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('MOMOI_INSTALL_DIR', 'install')
    provider = SherpaASRProvider()
    assert provider.model_path == str(tmp_path / 'install' / 'models' / 'asr')
    assert provider._engine is None


def test_windows_shell_respects_custom_model_and_provider_switches(monkeypatch, tmp_path):
    monkeypatch.setenv('MOMOI_INSTALL_DIR', str(tmp_path))
    monkeypatch.setattr('momoi.integrations.adapters.sherpa.sys.platform', 'win32')
    provider = SherpaASRProvider(model_path='/custom/models')
    assert provider.model_path == '/custom/models'
    assert provider._engine is None
    with pytest.raises(TypeError):
        SherpaASRProvider(endpoint='http://asr:8003', model_path='/custom/models')
    test_dashboard_switches_asr_without_cloud_credentials(tmp_path)


def test_builtin_model_paths_and_lazy_loading(monkeypatch, tmp_path):
    monkeypatch.delenv('MOMOI_INSTALL_DIR', raising=False)
    monkeypatch.delenv('MOMOI_ASR_MODEL_PATH', raising=False)
    monkeypatch.chdir(tmp_path)
    provider = SherpaASRProvider()
    assert provider.model_path == str(tmp_path / 'models/asr')
    assert provider._engine is None
    monkeypatch.setenv('MOMOI_ASR_MODEL_PATH', '/opt/momoi/models/asr')
    assert SherpaASRProvider().model_path == '/opt/momoi/models/asr'
    assert SherpaASRProvider(model_path='/custom').model_path == '/custom'


def test_legacy_asr_catalog_migration_preserves_options_and_input():
    import copy
    from momoi.integrations.configuration import managed_catalog
    raw = {'services': {'sherpa': {'adapter': 'sherpa', 'settings': {
        'endpoint': 'http://asr:8003', 'num_threads': 4}}},
        'bindings': {'asr': {'service': 'sherpa', 'enabled': False,
                            'options': {'trailing_silence': 0.6}}}}
    original = copy.deepcopy(raw)
    updated = managed_catalog(raw)
    assert raw == original
    binding = updated['bindings']['asr']
    assert binding['enabled'] is False
    assert binding['options'] == {'num_threads': 4, 'trailing_silence': 0.6}
    assert updated['services'][binding['service']]['settings'] == {}
    assert managed_catalog(updated) == updated
    raw['services']['sherpa']['settings']['endpoint'] = 'https://custom.example'
    assert 'endpoint' not in managed_catalog(raw)['bindings']['asr']['options']


def test_builtin_asr_reuses_engine_and_isolates_streams(monkeypatch):
    engines = []
    class Engine:
        def __init__(self, path, threads, silence):
            assert (path, threads, silence) == ('/models/asr', 3, 0.6)
            engines.append(self)
        def create_stream(self):
            return object()
        def feed(self, stream, pcm):
            return {'text': '识别结果', 'final': True}
        def transcribe(self, data):
            return '识别结果'
    monkeypatch.setattr('momoi.integrations.adapters.sherpa.SherpaEngine', Engine)
    async def run():
        provider = SherpaASRProvider(model_path='/models/asr', num_threads=3, trailing_silence=0.6)
        assert engines == []
        first, second = await asyncio.gather(provider.create_stream(), provider.create_stream())
        assert len(engines) == 1
        assert first.stream is not second.stream
        assert await provider.transcribe(AudioInput(b'wav', 'wav')) == '识别结果'
        await first.close()
        assert await second.feed(bytes(640)) == {'text': '识别结果', 'final': True}
        await second.close()
        await provider.close()
    asyncio.run(run())


def test_local_asr_schema_has_no_network_options():
    from momoi.integrations.registry import adapter_definition
    fields = adapter_definition('sherpa', 'asr').schema
    assert 'endpoint' not in fields
    assert 'timeout_seconds' not in fields


def test_legacy_network_timeout_does_not_break_local_catalog(tmp_path):
    from momoi.integrations.configuration import parse_provider_catalog
    raw = {'version': 1, 'services': {'asr': {'adapter': 'sherpa',
           'timeout_seconds': 30, 'settings': {'endpoint': 'https://old.example', 'num_threads': 3}}},
           'bindings': {'asr': {'service': 'asr', 'options': {'model_path': '/custom/asr', 'timeout_seconds': 10}}}}
    catalog = parse_provider_catalog(raw, tmp_path / 'providers.yaml')
    options = catalog.options_for('asr')
    assert 'endpoint' not in options and 'timeout_seconds' not in options
    assert options['model_path'] == '/custom/asr' and options['num_threads'] == 3
