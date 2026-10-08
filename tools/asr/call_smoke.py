"""Run in a test Momoi container; replay PCM through the real QQ call lane."""
import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import wave

from aiohttp import web
from momoi.config.manager import ConfigurationManager
from momoi.integrations.registry import ServiceRegistry
from momoi.qq_call.channel import QQCallChannel


async def run(audio_path, config_path, expected_text):
    with wave.open(str(audio_path)) as wav:
        assert wav.getframerate() == 16000
        pcm = wav.readframes(wav.getnframes()) + bytes(96000)
    playback_stops = []
    async def audio(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        ready = await ws.receive_json()
        assert ready['type'] == 'ready'
        await ws.send_json({'type': 'status', 'protocol_version': 1,
                           'session_id': 'local-asr-poc', 'generation': 0,
                           'phase': 'connected', 'ready': True})
        try:
            for offset in range(0, len(pcm), 640):
                await ws.send_bytes(pcm[offset:offset+640])
                await asyncio.sleep(.02)
            async for message in ws:
                pass
        except Exception:
            pass
        return ws
    async def stop_playback(request):
        playback_stops.append(await request.json())
        return web.json_response({'ok': True})
    app = web.Application()
    app.router.add_get('/v1/audio', audio)
    app.router.add_post('/v1/playback/stop', stop_playback)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    registry = ServiceRegistry(ConfigurationManager(config_path).validate().providers)
    provider = registry.asr
    assert type(provider).__name__ == 'SherpaASRProvider'
    channel = QQCallChannel(SimpleNamespace(bridge_url=f'http://127.0.0.1:{port}',
        bridge_token='poc-token', request_timeout_seconds=10), '123', provider, tts_enabled=True)
    received = asyncio.Event()
    events = []
    async def on_event(event):
        events.append(event)
        received.set()
    task = asyncio.create_task(channel.run(on_event, asyncio.Event()))
    try:
        await asyncio.wait_for(received.wait(), 20)
        assert len(events) == 1
        event = events[0]
        assert event.text == expected_text, (event.text, expected_text)
        assert event.channel == 'qq_call'
        assert event.delivery_context['call_session_id'] == 'local-asr-poc'
        assert playback_stops and channel.generation == 1
        print(json.dumps({'ok': True, 'provider': type(provider).__name__, 'owner_text': event.text,
                          'channel': event.channel, 'generation': channel.generation,
                          'playback_stop_called': True}, ensure_ascii=False))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await provider.close()
        await runner.cleanup()
        assert channel.asr_stream is None


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--config", type=Path, required=True, help="Isolated test workspace config.json")
    parser.add_argument("--expected-text", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.audio, args.config, args.expected_text))
