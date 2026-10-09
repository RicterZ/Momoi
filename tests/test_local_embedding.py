import asyncio
import copy
from pathlib import Path
import threading

import numpy as np
import pytest

from momoi.integrations.adapters import local_embedding as local
from momoi.integrations.configuration import parse_provider_catalog
from momoi.integrations.registry import ServiceRegistry


def test_local_encoder_shared_across_runtime_reload_without_network(tmp_path, monkeypatch):
    calls = []
    class Encoder:
        def embed(self, texts, **kwargs):
            calls.append(list(texts))
            return [np.array([3., 4.] + [0.] * 510) for _ in texts]
    loads = []
    def load(path):
        loads.append(path)
        return Encoder()
    monkeypatch.setattr(local, 'load_encoder', load)
    monkeypatch.setenv('MOMOI_EMBEDDING_MODEL_PATH', str(tmp_path))
    raw = {'version': 1, 'services': {'bge': {'adapter': 'local'}},
           'bindings': {'embedding': {'service': 'bge', 'enabled': True}}}
    catalog = parse_provider_catalog(raw, tmp_path / 'providers.yaml')
    first, second = ServiceRegistry(catalog), ServiceRegistry(catalog)
    assert first.embedding is second.embedding
    assert loads == []

    async def run():
        async with first:
            query, documents = await asyncio.gather(first.embedding.encode(['查询'], query=True),
                                                    first.embedding.encode(['文档', 'document'], query=False))
            assert np.allclose(query[0][:2], [.6, .8])
            assert len(documents) == 2
        async with second:
            assert (await second.embedding.health())[0]
            assert await second.embedding.encode([], query=True) == []
    asyncio.run(run())
    assert loads == [tmp_path.resolve()]
    assert len(calls) == 3


@pytest.mark.parametrize('vectors', [[], [[0.] * 512], [[float('nan')] * 512], [[1.] * 511]])
def test_local_encoder_rejects_bad_output(tmp_path, monkeypatch, vectors):
    class Encoder:
        def embed(self, texts, **kwargs):
            return vectors
    monkeypatch.setattr(local, 'load_encoder', lambda path: Encoder())
    with pytest.raises(ValueError):
        asyncio.run(local.LocalEmbedding(tmp_path).encode(['query'], query=True))


def test_cancelled_inference_does_not_allow_concurrent_onnx_calls(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    class Encoder:
        def embed(self, texts, **kwargs):
            calls.append(texts)
            if len(calls) == 1:
                entered.set()
                assert release.wait(5)
            return [np.ones(512)]
    monkeypatch.setattr(local, 'load_encoder', lambda path: Encoder())
    provider = local.LocalEmbedding(tmp_path)
    async def run():
        first = asyncio.create_task(provider.encode(['first'], query=True))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(provider.encode(['second'], query=False))
            await asyncio.sleep(.03)
            assert calls == [['first']]
            release.set()
            assert len(await second) == 1
        finally:
            release.set()
    asyncio.run(run())
    assert calls == [['first'], ['second']]


def test_legacy_sidecar_migration_preserves_custom_services_and_disable(tmp_path):
    raw = {'version': 1, 'services': {'shared': {'adapter': 'openai', 'settings': {
        'endpoint': 'http://embedding:8002/v1/embeddings'}}},
        'bindings': {'embedding': {'service': 'shared', 'enabled': False}}}
    before = copy.deepcopy(raw)
    path = tmp_path / 'providers.yaml'
    migrated = parse_provider_catalog(raw, path)
    assert migrated.adapter_for('embedding') == 'local'
    assert not migrated.enabled('embedding')
    assert raw == before
    raw['services']['shared']['settings']['endpoint'] = 'https://custom.example/v1/embeddings'
    assert parse_provider_catalog(raw, path).adapter_for('embedding') == 'openai'
    raw['services']['shared']['settings'].update(endpoint='http://embedding:8002/v1/embeddings', model='custom')
    assert parse_provider_catalog(raw, path).adapter_for('embedding') == 'openai'
