"""NapCat-side media service. No model, ASR credentials, or private memories."""
import asyncio
import array
import contextlib
import hmac
import json
import os
from pathlib import Path
import shutil
import uuid
import time
import sys
import traceback
from collections import OrderedDict

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web


async def stop_process(process):
    if process and process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


class MediaBroker:
    def __init__(self, token, *, native_url='http://127.0.0.1:6110', host_url='http://127.0.0.1:6111', audio=None):
        if len(token.encode()) < 32:
            raise ValueError('Bridge token must contain at least 32 bytes')
        self.token = token
        self.native_url, self.host_url = native_url, host_url
        self.audio = audio
        self.capture_command = audio.capture_command if audio else (
            'parec', '--raw', '--device=maibot_qq_speaker.monitor',
            '--format=s16le', '--rate=16000', '--channels=1')
        self.playback_command = audio.playback_command if audio else (
            'pacat', '--playback', '--raw', '--device=maibot_qq_mic',
            '--format=s16le', '--rate=24000', '--channels=1', '--latency-msec=50')
        self.http = None
        self.ws = None
        self.owner = ''
        self.armed = False
        self.session_id = ''
        self.invite = ''
        self.generation = 0
        self.status = {'protocol_version': 1, 'ready': False, 'phase': 'unavailable', 'error': 'Starting'}
        self.capture = self.capture_task = self.play_process = self.poll_task = None
        self.capture_stats = {}
        self.capture_muted_until = 0.0
        self.audio_error = ""
        self.audio_retry_at = 0.0
        self.play_lock = asyncio.Lock()
        self.receipts = OrderedDict()

    def valid(self, session_id, generation):
        return bool(self.ws is not None and not self.ws.closed and self.armed and self.session_id
                    and self.status.get('phase') == 'connected'
                    and session_id == self.session_id and generation == self.generation)

    async def native(self, method, path, **kwargs):
        async with self.http.request(method, self.native_url + path,
                    headers={'Authorization': 'Bearer ' + self.token}, **kwargs) as response:
            response.raise_for_status()
            return await response.json()

    async def dependencies(self):
        try:
            plugin = (await self.native('GET', '/v1/status'))['data']
            async with self.http.get(self.host_url + '/v1/status', headers={
                    'Authorization': 'Bearer ' + self.token}) as response:
                response.raise_for_status()
                host = (await response.json())['data']
            if self.audio is not None:
                audio_ready = self.audio.ready
            else:
                process = await asyncio.create_subprocess_exec('pactl', 'info',
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                try:
                    pulse = await asyncio.wait_for(process.wait(), 2) == 0
                finally:
                    await stop_process(process)
                audio_ready = pulse and all(shutil.which(c) for c in ('parec', 'pacat', 'ffmpeg'))
            deps = {'bridge': bool(plugin.get('listenerRegistered') and plugin.get('avHost', {}).get('loginSucceeded')),
                    'av_host': bool(host.get('ready') and host.get('pluginFound')),
                    'audio': bool(audio_ready)}
            return deps, plugin.get('call') or {}
        except (OSError, ValueError, KeyError, asyncio.TimeoutError, __import__('aiohttp').ClientError):
            return {'bridge': False, 'av_host': False, 'audio': False}, {}

    async def invalidate(self):
        self.generation += 1
        # Invalidate delivery immediately, then cancel the reader before stopping
        # its process. Normal hangup must not turn EOF into permanent disarming.
        self.session_id = ''
        if self.capture_task:
            self.capture_task.cancel()
            await asyncio.gather(self.capture_task, return_exceptions=True)
        await stop_process(self.play_process)
        await stop_process(self.capture)
        self.capture_task = self.capture = None

    async def send_status(self):
        if self.ws is not None and not self.ws.closed:
            try:
                await asyncio.wait_for(self.ws.send_json({'type': 'status', **self.status,
                    'session_id': self.session_id, 'generation': self.generation}), 2)
            except (ConnectionError, asyncio.TimeoutError):
                self.armed = False

    async def poll(self):
        while True:
            if self.audio is not None and hasattr(self.audio, "configuration_changed") and self.audio.configuration_changed():
                await self.invalidate()
                self.audio.reload_configuration()
            deps, call = await self.dependencies()
            if self.audio is not None and not self.audio.ready and deps['av_host'] and time.monotonic() >= self.audio_retry_at:
                try:
                    await self.audio.prepare(self.http, self.host_url, self.token)
                    self.capture_command = self.audio.capture_command
                    self.playback_command = self.audio.playback_command
                    self.audio_error = ''
                except Exception as error:
                    message = '音频设备未就绪：' + str(error)
                    if message != self.audio_error:
                        print(json.dumps({"event": "qq_call_audio_prepare_failed", "error": str(error),
                            "traceback": traceback.format_exc()}, ensure_ascii=False), file=sys.stderr, flush=True)
                    self.audio_error = message
                self.audio_retry_at = time.monotonic() + 5
                deps, call = await self.dependencies()
            ready = all(deps.values())
            enabled = bool(ready and self.armed and self.ws is not None and not self.ws.closed)
            if self.owner:
                with contextlib.suppress(Exception):
                    await self.native('POST', '/v1/momoi/ready', json={
                        'ownerUin': self.owner, 'ready': enabled})
            phase = call.get('phase', 'idle') if ready else 'unavailable'
            connected = enabled and phase == 'connected' and call.get('callerUin') == self.owner
            invite = str(call.get('inviteAt') or '')
            if self.session_id and (not connected or invite != self.invite):
                await self.invalidate()
            if connected and not self.session_id:
                self.invite = invite
                self.session_id = uuid.uuid4().hex
                self.capture_stats = {'frames': 0, 'bytes': 0, 'peak': 0}
                self.generation = 0
            self.status = {'protocol_version': 1, 'ready': ready, 'dependencies': deps,
                'phase': phase, 'client_connected': self.ws is not None and not self.ws.closed,
                'audio_devices': getattr(self.audio, 'device_selection', {}),
                'audio_warnings': getattr(self.audio, 'warnings', []),
                'half_duplex': getattr(self.audio, 'half_duplex', False),
                'auto_answer_ready': enabled, 'capture': dict(self.capture_stats), 'error': '' if ready else (self.audio_error or
                    ('等待 QQ 登录及通话插件就绪' if not deps['bridge'] else
                     ('等待 AVSDK 宿主就绪' if not deps['av_host'] else '虚拟音频设备未就绪')))}
            await self.send_status()
            if connected and (self.capture_task is None or self.capture_task.done()):
                self.capture_task = asyncio.create_task(self.capture_audio(self.session_id))
            await asyncio.sleep(0.75)

    async def capture_audio(self, session_id):
        process = None
        next_report = time.monotonic()
        try:
            print(json.dumps({"event": "qq_call_capture_started", "session_id": session_id}), flush=True)
            process = await asyncio.create_subprocess_exec(*self.capture_command,
                stdout=asyncio.subprocess.PIPE, stderr=None)
            self.capture = process
            while self.session_id == session_id and self.ws is not None and not self.ws.closed:
                frame = await process.stdout.readexactly(640)
                samples = array.array("h", frame)
                if sys.byteorder != "little":
                    samples.byteswap()
                peak = max((abs(sample) for sample in samples), default=0)
                self.capture_stats["frames"] = self.capture_stats.get("frames", 0) + 1
                self.capture_stats["bytes"] = self.capture_stats.get("bytes", 0) + len(frame)
                self.capture_stats["peak"] = max(self.capture_stats.get("peak", 0), peak)
                if time.monotonic() >= next_report:
                    print(json.dumps({"event": "qq_call_capture_progress", "session_id": session_id,
                          **self.capture_stats}), flush=True)
                    next_report = time.monotonic() + 5
                if getattr(self.audio, "half_duplex", False):
                    if self.play_process is not None:
                        self.capture_muted_until = time.monotonic() + .35
                    if time.monotonic() < self.capture_muted_until:
                        frame = b"\0" * 640
                await asyncio.wait_for(self.ws.send_bytes(frame), 2)
        except (OSError, ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError) as error:
            print(json.dumps({"event": "qq_call_capture_failed", "session_id": session_id,
                  "error_type": type(error).__name__, "error": str(error)}), flush=True)
            # Unexpected capture failures close the connection so Momoi can
            # reconnect and re-arm, rather than leave a silently disabled socket.
            if self.session_id == session_id and self.ws is not None and not self.ws.closed:
                await self.ws.close(code=1011, message=b'Audio capture failed')
        finally:
            await stop_process(process)
            if self.capture is process:
                self.capture = None

    async def socket(self, request):
        if self.ws is not None and not self.ws.closed:
            raise web.HTTPConflict(text='A Momoi session is already connected')
        ws = web.WebSocketResponse(heartbeat=5, max_msg_size=4096)
        await ws.prepare(request)
        self.ws = ws
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    value = json.loads(message.data)
                    owner = value.get('owner_qq')
                    if value.get('type') != 'ready' or not isinstance(owner, str) or not owner.isascii() or not owner.isdigit() or type(value.get('ready')) is not bool:
                        raise ValueError()
                    self.owner, self.armed = owner, value['ready']
                except (ValueError, TypeError, AttributeError):
                    await ws.close(code=1008, message=b'Invalid readiness message')
                    break
        finally:
            self.armed = False
            await self.invalidate()
            with contextlib.suppress(Exception):
                if self.owner:
                    await self.native('POST', '/v1/momoi/ready', json={'ownerUin': self.owner, 'ready': False})
            self.ws = None
        return ws

    async def stop_playback(self, request):
        value = await request.json()
        if value.get('session_id') != self.session_id or not self.session_id:
            raise web.HTTPConflict(text='Call session ended')
        generation = value.get('generation')
        if type(generation) is not int or generation < self.generation:
            raise web.HTTPConflict(text='Stale call generation')
        self.generation = generation
        await stop_process(self.play_process)
        return web.json_response({'ok': True})

    async def play_stream(self, request):
        session_id = request.headers.get('X-Call-Session', '')
        utterance = request.headers.get('X-Call-Utterance', '')
        try:
            generation = int(request.headers['X-Call-Generation'])
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text='Missing generation')
        if not utterance or len(utterance) > 128:
            raise web.HTTPBadRequest(text='Invalid utterance')
        key = (session_id, utterance)
        async with self.play_lock:
            if key in self.receipts:
                return web.json_response(self.receipts[key])
            if not self.valid(session_id, generation):
                raise web.HTTPConflict(text='Stale call')
            started = time.monotonic()
            first_ms = None
            played = 0
            pending = b''
            process = None
            complete = False
            try:
                async def playback():
                    nonlocal played, pending, process, first_ms
                    async for chunk in request.content.iter_chunked(960):
                        if not self.valid(session_id, generation):
                            return
                        pending += chunk
                        if played + len(pending) > 48000 * 120:
                            raise ValueError('Audio exceeds limit')
                        while len(pending) >= 960:
                            await write_frame(pending[:960])
                            pending = pending[960:]
                    if pending:
                        if len(pending) % 2:
                            raise ValueError('Incomplete PCM sample')
                        await write_frame(pending)
                        pending = b''
                    if process is not None:
                        process.stdin.close()
                        await asyncio.wait_for(process.wait(), 3)
                async def write_frame(frame):
                    nonlocal process, played, first_ms
                    if not self.valid(session_id, generation):
                        raise ConnectionError('Call superseded')
                    if process is None:
                        process = await asyncio.create_subprocess_exec(*self.playback_command, stdin=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL)
                        self.play_process = process
                    process.stdin.write(frame)
                    await process.stdin.drain()
                    if first_ms is None:
                        first_ms = round((time.monotonic() - started) * 1000)
                        print(json.dumps({'event': 'qq_call_playback_first_frame', 'session_id': session_id,
                            'utterance_id': utterance, 'elapsed_ms': first_ms}), flush=True)
                    played += len(frame)
                    await asyncio.sleep(len(frame) / 48000)
                await asyncio.wait_for(playback(), 150)
                complete = bool(played and process and process.returncode == 0 and self.valid(session_id, generation))
            except (OSError, ConnectionError, ValueError, asyncio.TimeoutError):
                pass
            finally:
                await stop_process(process)
                if self.play_process is process:
                    self.play_process = None
            result = {'ok': complete, 'state': 'played' if complete else 'interrupted',
                'played_ms': round(played / 48), 'first_frame_ms': first_ms,
                'elapsed_ms': round((time.monotonic() - started) * 1000),
                'reason': None if complete else ('call_ended' if session_id != self.session_id
                    else 'owner_speech' if generation != self.generation else 'playback_failed')}
            self.receipts[key] = result
            while len(self.receipts) > 128:
                self.receipts.popitem(last=False)
            return web.json_response(result)

    async def play(self, request):
        session_id = request.headers.get('X-Call-Session', '')
        utterance = request.headers.get('X-Call-Utterance', '')
        try:
            generation = int(request.headers['X-Call-Generation'])
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text='Missing call generation')
        fmt = request.headers.get('X-Audio-Format', '')
        if fmt not in {'mp3', 'wav', 'opus', 'silk'} or not utterance or len(utterance) > 128:
            raise web.HTTPBadRequest(text='Invalid playback')
        data = await request.read()
        if not data:
            raise web.HTTPBadRequest(text='Empty audio')
        key = (session_id, utterance)
        async with self.play_lock:
            if key in self.receipts:
                return web.json_response(self.receipts[key])
            if not self.valid(session_id, generation):
                raise web.HTTPConflict(text='Call ended or utterance superseded')
            # Decode in the transport container; normal Momoi images need no FFmpeg.
            decoder = await asyncio.create_subprocess_exec('ffmpeg', '-v', 'error', '-i', 'pipe:0',
                '-t', '120', '-f', 's16le', '-ar', '24000', '-ac', '1', 'pipe:1',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            async def decode():
                async def feed():
                    try:
                        decoder.stdin.write(data)
                        await decoder.stdin.drain()
                    except (BrokenPipeError, ConnectionError):
                        pass
                    finally:
                        decoder.stdin.close()
                writer = asyncio.create_task(feed())
                output = bytearray()
                try:
                    while True:
                        chunk = await decoder.stdout.read(65536)
                        if not chunk:
                            break
                        output.extend(chunk)
                        if len(output) > 24000 * 2 * 120:
                            raise web.HTTPBadRequest(text='Audio exceeds two minutes')
                    await writer
                    await decoder.wait()
                    return bytes(output)
                finally:
                    writer.cancel()
                    await asyncio.gather(writer, return_exceptions=True)
            try:
                pcm = await asyncio.wait_for(decode(), 30)
            except asyncio.TimeoutError:
                raise web.HTTPBadRequest(text='Audio decode timed out')
            finally:
                await stop_process(decoder)
            if decoder.returncode != 0 or not pcm or len(pcm) > 24000 * 2 * 120:
                raise web.HTTPBadRequest(text='Audio decode failed or exceeds two minutes')
            if not self.valid(session_id, generation):
                raise web.HTTPConflict(text='Call ended or utterance superseded')
            process = await asyncio.create_subprocess_exec(*self.playback_command, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            self.play_process = process
            played = 0
            try:
                # Pace writes so cancellation also stops queued audio quickly.
                for offset in range(0, len(pcm), 960):
                    if not self.valid(session_id, generation) or process.returncode is not None:
                        break
                    frame = pcm[offset:offset + 960]
                    process.stdin.write(frame)
                    await process.stdin.drain()
                    played += len(frame)
                    await asyncio.sleep(len(frame) / 48000)
                if played == len(pcm) and self.valid(session_id, generation):
                    process.stdin.close()
                    await asyncio.wait_for(process.wait(), 3)
            except (BrokenPipeError, ConnectionError, asyncio.TimeoutError):
                pass
            finally:
                await stop_process(process)
                self.play_process = None
            completed = played == len(pcm) and process.returncode == 0 and self.valid(session_id, generation)
            result = {'ok': completed, 'state': 'played' if completed else 'interrupted',
                      'played_ms': round(played / 48), 'session_id': session_id,
                      'reason': None if completed else ('call_ended' if session_id != self.session_id
                          else 'owner_speech' if generation != self.generation else 'playback_failed')}
            self.receipts[key] = result
            while len(self.receipts) > 128:
                self.receipts.popitem(last=False)
            return web.json_response(result)

    async def startup(self, app):
        self.http = ClientSession(timeout=ClientTimeout(total=3))
        try:
            self.poll_task = asyncio.create_task(self.poll())
        except BaseException:
            await self.http.close()
            raise

    async def cleanup(self, app):
        self.armed = False
        if self.owner:
            with contextlib.suppress(Exception):
                await self.native('POST', '/v1/momoi/ready', json={'ownerUin': self.owner, 'ready': False})
        if self.ws is not None:
            await self.ws.close()
        await self.invalidate()
        if self.poll_task:
            self.poll_task.cancel()
            await asyncio.gather(self.poll_task, return_exceptions=True)
        if self.http:
            await self.http.close()
        if self.audio is not None and hasattr(self.audio, "close"):
            self.audio.close()

    def app(self):
        @web.middleware
        async def authenticate(request, handler):
            if request.path != '/healthz' and not hmac.compare_digest(
                    request.headers.get('Authorization', '').encode(), ('Bearer ' + self.token).encode()):
                raise web.HTTPUnauthorized()
            return await handler(request)
        app = web.Application(middlewares=[authenticate], client_max_size=20 * 1024 * 1024)
        async def health(request):
            return web.json_response({'ok': bool(self.poll_task and not self.poll_task.done()), 'ready': bool(self.status.get('ready'))},
                                     status=200 if self.poll_task and not self.poll_task.done() else 503)
        async def status(request):
            return web.json_response(self.status)
        app.router.add_get('/healthz', health)
        app.router.add_get('/v1/status', status)
        app.router.add_get('/v1/audio', self.socket)
        app.router.add_post('/v1/playback', self.play)
        app.router.add_post('/v1/playback/stream', self.play_stream)
        app.router.add_post('/v1/playback/stop', self.stop_playback)
        if self.audio is not None:
            async def shutdown(request):
                def exit_server():
                    raise web.GracefulExit()
                asyncio.get_running_loop().call_later(0.1, exit_server)
                return web.json_response({'ok': True})
            app.router.add_post('/v1/shutdown', shutdown)

        app.on_startup.append(self.startup)
        app.on_cleanup.append(self.cleanup)
        return app


def main():
    runtime = Path(os.getenv('QQ_CALL_RUNTIME', '/app/qq-call'))
    token_path = Path(os.getenv('QQ_CALL_TOKEN_FILE', str(runtime / 'runtime/control.token')))
    options = dict(native_url=os.getenv('QQ_CALL_NATIVE_URL', 'http://127.0.0.1:6110'),
                   host_url=os.getenv('QQ_CALL_HOST_URL', 'http://127.0.0.1:6111'))
    port = int(os.getenv('QQ_CALL_PORT', '6112'))
    windows_bridge = os.getenv('QQ_CALL_WINDOWS_BRIDGE')
    if sys.platform == 'win32':
        if not windows_bridge:
            raise RuntimeError('Windows QQ call audio adapter is not configured')
        sys.path.insert(0, str(Path(windows_bridge).resolve() / 'windows'))
        from .windows_audio import DeferredWindowsAudio
        audio = DeferredWindowsAudio(runtime, windows_bridge)
        try:
            broker = MediaBroker(token_path.read_text().strip(), audio=audio, **options)
            web.run_app(broker.app(), host='127.0.0.1', port=port, access_log=None)
        finally:
            audio.close()
    else:
        broker = MediaBroker(token_path.read_text().strip(), **options)
        web.run_app(broker.app(), host='0.0.0.0', port=port, access_log=None)



if __name__ == '__main__':
    main()
