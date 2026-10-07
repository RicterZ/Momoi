import asyncio
from contextlib import aclosing
import subprocess

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from momoi.integrations.adapters.vocu import VocuTTSProvider
from momoi.integrations.contracts.tts import TTSError
from momoi.integrations.registry import adapter_definition, ServiceRegistry
from momoi.integrations.fields import normalize_fields, redact_fields
from momoi.config.manager import ConfigurationManager
from momoi.config.workspace import bootstrap


def test_vocu_metadata_and_dashboard_switch(tmp_path):
    path = tmp_path / 'config.json'
    bootstrap(path)
    manager = ConfigurationManager(path)
    schema = adapter_definition('vocu', 'tts').schema
    assert schema['voice_id']['required']
    assert schema['api_key']['secret']
    assert 'reference_id' not in schema and 'format' not in schema
    options = normalize_fields(schema, {'api_key': 'synthetic', 'voice_id': 'voice-test'})
    assert redact_fields(schema, options)['api_key'] == {'$secret': 'keep'}
    for adapter, values in [('fish', {'api_key': 'fish-key'}),
                            ('vocu', {'api_key': 'vocu-key', 'voice_id': 'voice-test'})]:
        snapshot = manager.save_binding('tts', {'adapter': adapter, 'enabled': True,
            'options': values}, manager.revision())
        assert snapshot['capabilities']['tts']['adapter'] == adapter
        provider = ServiceRegistry(manager.validate().providers).tts
        assert (isinstance(provider, VocuTTSProvider)) == (adapter == 'vocu')
    assert provider.voice_id == 'voice-test'


@pytest.mark.parametrize('options', [
    {'api_key': ''}, {'voice_id': ''}, {'api_key': 'key\nsecret'},
    {'base_url': 'https://user:secret@example.test'}, {'timeout_seconds': 0},
    {'max_audio_bytes': True}, {'speech_rate': 3}, {'flash': 1}, {'preset': 'unknown'},
])
def test_vocu_rejects_invalid_configuration(options):
    with pytest.raises(ValueError):
        VocuTTSProvider(**{'api_key': 'synthetic', 'voice_id': 'test', **options})


def test_vocu_generation_and_audio_download():
    async def scenario():
        requests = []
        async def generate(request):
            requests.append((request.headers.get('Authorization'), await request.json()))
            return web.json_response({'status': 200, 'data': {'audio': str(server.make_url('/audio'))}})
        async def audio(request):
            assert 'Authorization' not in request.headers
            return web.Response(body=b'ID3-synthetic', content_type='audio/mpeg')
        app = web.Application()
        app.router.add_post('/api/tts/simple-generate', generate)
        app.router.add_get('/audio', audio)
        async with TestServer(app) as server:
            provider = VocuTTSProvider(api_key='synthetic', voice_id='voice-test',
                prompt_id='style-test', preset='stable', speech_rate=.8, flash=True,
                base_url=str(server.make_url('/api')))
            result = await provider.synthesize('synthetic text')
            assert result.data == b'ID3-synthetic' and result.format == 'mp3'
        assert requests == [('Bearer synthetic', {'voiceId': 'voice-test', 'text': 'synthetic text',
            'promptId': 'style-test', 'preset': 'stable', 'speechRate': .8, 'language': 'auto',
            'flash': True, 'vivid': False, 'stream': False, 'srt': False})]
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['http', 'status', 'json', 'url', 'mime', 'empty', 'size'])
def test_vocu_failures_are_bounded_and_do_not_expose_response(case):
    async def scenario():
        calls = []
        async def generate(request):
            calls.append(await request.json())
            if case == 'http':
                return web.Response(status=403, text='private-key synthetic text')
            if case == 'json':
                return web.Response(text='not-json private-key')
            return web.json_response({'status': 403 if case == 'status' else 200,
                'data': {'audio': 'file:///private-key' if case == 'url' else str(server.make_url('/audio'))}})
        async def audio(request):
            return web.Response(body=b'' if case == 'empty' else b'12345678',
                content_type='text/html' if case == 'mime' else 'audio/mpeg')
        app = web.Application()
        app.router.add_post('/api/tts/simple-generate', generate)
        app.router.add_get('/audio', audio)
        async with TestServer(app) as server:
            provider = VocuTTSProvider(api_key='private-key', voice_id='voice-test',
                base_url=str(server.make_url('/api')), max_audio_bytes=4 if case == 'size' else 100)
            with pytest.raises(TTSError) as error:
                await provider.synthesize('synthetic text')
            assert 'private-key' not in str(error.value)
            assert 'synthetic text' not in str(error.value)
            assert len(calls) == 1
    asyncio.run(scenario())


def test_vocu_stream_decodes_before_download_finishes():
    audio = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=8', '-f', 'mp3', 'pipe:1'],
        check=True, capture_output=True).stdout
    async def scenario():
        release = asyncio.Event()
        requests = []
        async def generate(request):
            requests.append(await request.json())
            return web.json_response({'status': 200, 'data': {
                'audio': str(server.make_url('/unused')), 'streamUrl': str(server.make_url('/audio'))}})
        async def download(request):
            assert 'Authorization' not in request.headers
            response = web.StreamResponse(headers={'Content-Type': 'audio/mpeg'})
            await response.prepare(request)
            await response.write(audio[:-4096])
            await release.wait()
            await response.write(audio[-4096:])
            return response
        app = web.Application()
        app.router.add_post('/api/tts/simple-generate', generate)
        app.router.add_get('/audio', download)
        async with TestServer(app) as server:
            provider = VocuTTSProvider(api_key='synthetic', voice_id='voice-test',
                base_url=str(server.make_url('/api')))
            try:
                async with aclosing(provider.stream_pcm('synthetic text')) as stream:
                    first = await asyncio.wait_for(anext(stream), 3)
                    assert first and len(first) % 2 == 0
                    assert not release.is_set()
                    release.set()
                    pcm = first + b''.join([chunk async for chunk in stream])
                    assert 7 * 48000 < len(pcm) < 9 * 48000
                assert requests[0]['stream'] is True
            finally:
                release.set()
    asyncio.run(scenario())


def test_vocu_stream_cancellation_closes_decoder(monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        processes = []
        original_spawn = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            process = await original_spawn(*args, **kwargs)
            processes.append(process)
            return process

        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)

        async def generate(request):
            entered.set()
            await release.wait()
            return web.json_response({'status': 403})

        app = web.Application()
        app.router.add_post('/api/tts/simple-generate', generate)
        async with TestServer(app) as server:
            provider = VocuTTSProvider(api_key='synthetic', voice_id='voice-test',
                base_url=str(server.make_url('/api')))
            stream = provider.stream_pcm('synthetic text')
            task = asyncio.create_task(anext(stream))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
                assert len(processes) == 1 and processes[0].returncode is not None
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await stream.aclose()
    asyncio.run(scenario())


def test_vocu_debug_reports_audio_stages_without_signed_urls(caplog):
    import logging
    async def scenario():
        async def generate(request):
            return web.json_response({'status': 200, 'data': {
                'audio': str(server.make_url('/audio?signature=private-signed-token'))}})
        async def audio(request):
            return web.Response(body=b'mp3-test', content_type='audio/mpeg')
        app = web.Application()
        app.router.add_post('/api/tts/simple-generate', generate)
        app.router.add_get('/audio', audio)
        async with TestServer(app) as server:
            provider = VocuTTSProvider(api_key='private-api-key', voice_id='voice-test',
                base_url=str(server.make_url('/api')))
            await provider.synthesize('private-input-text')
    with caplog.at_level(logging.DEBUG, logger='momoi.integrations.adapters.vocu'):
        asyncio.run(scenario())
    records = '\n'.join(record.getMessage() for record in caplog.records
                        if record.name == 'momoi.integrations.adapters.vocu')
    for event in ('vocu_tts_request', 'vocu_tts_api_response', 'vocu_audio_download_started',
                  'vocu_audio_response', 'vocu_audio_first_chunk', 'vocu_audio_download_finished'):
        assert event in records
    for secret in ('private-api-key', 'private-input-text', 'private-signed-token'):
        assert secret not in records
