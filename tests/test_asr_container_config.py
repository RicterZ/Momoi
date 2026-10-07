"""Container endpoint settings must match the provider configuration."""
import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient


def test_container_uses_request_settings_and_preserves_active_stream(monkeypatch):
    spec = importlib.util.spec_from_file_location('asr_config_test_app', Path(__file__).parents[1] / 'infra/asr/app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engines = []

    class Engine:
        def __init__(self, path, threads, silence):
            self.settings = (threads, silence)
            engines.append(self)
        def create_stream(self):
            return object()
        def feed(self, state, pcm, finish=False):
            return {'text': str(self.settings), 'final': finish}
        def transcribe(self, data):
            return str(self.settings)

    monkeypatch.setattr(module, 'SherpaEngine', Engine)
    # Legacy environment overrides must have no effect.
    monkeypatch.setenv('ASR_TRAILING_SILENCE', '1.2')
    monkeypatch.setenv('ASR_NUM_THREADS', '9')
    with TestClient(module.app) as client:
        assert engines[0].settings == (2, 0.8)
        params = {'num_threads': 3, 'trailing_silence': 0.6}
        with client.websocket_connect('/v1/stream?num_threads=3&trailing_silence=0.6') as ws:
            ws.send_bytes(bytes(640))
            assert ws.receive_json()['text'] == '(3, 0.6)'
            assert client.post('/v1/transcribe', content=b'wav', params=params).json()['text'] == '(3, 0.6)'
            assert len(engines) == 2  # Same settings reuse the loaded model.
            assert client.get('/healthz').json()['trailing_silence'] == 0.6
            assert client.post('/v1/transcribe', content=b'wav').json()['text'] == '(2, 0.8)'
            ws.send_bytes(bytes(640))
            assert ws.receive_json()['text'] == '(3, 0.6)'
        assert client.post('/v1/transcribe?trailing_silence=0', content=b'wav').status_code == 422
        assert client.post('/v1/transcribe?num_threads=17', content=b'wav').status_code == 422
