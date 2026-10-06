"""One owner phone lane using the ordinary Planner and delivery interfaces."""
import asyncio
import contextlib
import logging
import time
import uuid
from collections import OrderedDict

import aiohttp

from ..channel import SendInterrupted, SendRejected
from ..integrations.contracts.asr import ASRError, AudioInput
from ..integrations.contracts.tts import TTSError
from ..models import IncomingMessage
from ..observability.events import log_event
from .audio import Segmenter, wav_bytes
from .speech import bubble_pcm

logger = logging.getLogger('momoi.qq_call')


class QQCallChannel:
    name = 'qq_call'
    dialogue_channel = 'napcat'
    quiet_seconds = .05
    max_batch_seconds = .25
    required_reply_mode = 'voice'

    def __init__(self, config, owner_qq, asr, *, tts_enabled, interrupt=None):
        self.config, self.owner_qq, self.asr = config, owner_qq, asr
        self.providers_ready = bool(asr and tts_enabled)
        self.interrupt = interrupt
        self.status = {'phase': 'unavailable', 'error': '等待 Bridge 连接' if self.providers_ready else '需要启用 ASR 和 TTS'}
        self.session_id = ''
        self.generation = 0
        self.segmenter = Segmenter()
        self.http = self.ws = None
        self.queue = asyncio.Queue(maxsize=8)
        self.pending_stop = None
        self.sent = OrderedDict()

    def routing_context(self):
        return {'call_session_id': self.session_id, 'call_generation': self.generation}

    def session_valid(self, context):
        return bool(context and self.session_id and self.status.get('phase') == 'connected'
                    and context.get('call_session_id') == self.session_id)

    def context_valid(self, context):
        return self.session_valid(context) and context.get('call_generation') == self.generation

    def message_current(self, event):
        return self.context_valid(event.delivery_context)

    async def end_session(self):
        old = self.session_id
        self.session_id = ''
        self.generation += 1
        self.segmenter.reset()
        if old:
            log_event(logger, logging.INFO, 'qq_call_session_ended', channel=self.name, session_id=old)
            if self.interrupt:
                self.interrupt('call_ended')
        while not self.queue.empty():
            self.queue.get_nowait()

    async def stop_playback(self, context):
        if self.http is None:
            return
        with contextlib.suppress(aiohttp.ClientError, asyncio.TimeoutError):
            async with self.http.post(self.config.bridge_url + '/v1/playback/stop',
                headers=self.headers, json={'session_id': context['call_session_id'],
                'generation': context['call_generation']},
                timeout=aiohttp.ClientTimeout(total=self.config.request_timeout_seconds)) as response:
                response.raise_for_status()

    @property
    def headers(self):
        return {'Authorization': 'Bearer ' + self.config.bridge_token}

    async def receive_audio(self, on_event):
        async for message in self.ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                value = message.json()
                if value.get('type') != 'status':
                    continue
                if value.get('protocol_version') != 1:
                    raise ValueError('Unsupported QQ call protocol')
                sid = str(value.get('session_id') or '')
                if sid != self.session_id:
                    await self.end_session()
                    self.session_id = sid
                    self.generation = int(value.get('generation') or 0)
                self.status = {k: value[k] for k in ('phase', 'ready', 'error', 'dependencies') if k in value}
            elif message.type == aiohttp.WSMsgType.BINARY and self.session_id:
                _, pcm = self.segmenter.feed(message.data)
                if pcm:
                    context = {**self.routing_context(), 'segment_finished_at': time.time()}
                    log_event(logger, logging.INFO, 'qq_call_segment_finished', channel=self.name,
                              session_id=self.session_id, audio_ms=len(pcm) // 32)
                    item = (context, wav_bytes(pcm))
                    if self.queue.full():
                        self.queue.get_nowait()
                    self.queue.put_nowait(item)
            elif message.type == aiohttp.WSMsgType.ERROR:
                break

    async def transcribe(self, on_event):
        while True:
            context, audio = await self.queue.get()
            if not self.session_valid(context):
                continue
            # Queued audio can predate a previous successful recognition; bind the
            # ASR request to the current generation, not its capture generation.
            context = {**context, **self.routing_context()}
            started = time.monotonic()
            try:
                text = await self.asr.transcribe(AudioInput(audio, 'wav'))
            except ASRError:
                self.status = {'phase': 'error', 'error': 'ASR 请求失败，请检查语音服务与额度'}
                # Fail closed: stop admitting calls until the runtime is reconfigured.
                self.providers_ready = False
                await self.ws.close()
                return
            log_event(logger, logging.INFO, 'qq_call_asr', channel=self.name,
                      session_id=context['call_session_id'], recognized=bool(text.strip()),
                      elapsed_ms=round((time.monotonic() - started) * 1000),
                      queue_wait_ms=round((time.time() - context.get('segment_finished_at', time.time())) * 1000
                                          - (time.monotonic() - started) * 1000))
            if text.strip() and self.context_valid(context):
                self.generation += 1
                context = {**context, **self.routing_context(), 'recognized_at': time.time()}
                log_event(logger, logging.INFO, 'qq_call_interrupt', channel=self.name,
                          session_id=self.session_id, generation=self.generation, reason='recognized_speech')
                if self.interrupt:
                    self.interrupt('owner_speech')
                if self.pending_stop:
                    self.pending_stop.cancel()
                    await asyncio.gather(self.pending_stop, return_exceptions=True)
                self.pending_stop = asyncio.create_task(self.stop_playback(context))
                await self.pending_stop
                if not self.context_valid(context):
                    continue
                event_id = 'qq-call:' + context['call_session_id'] + ':' + uuid.uuid4().hex
                now = time.time()
                await on_event(IncomingMessage(event_id, event_id, text.strip(), now, now,
                                               channel=self.name, delivery_context=context))

    async def run(self, on_event, stop):
        if not self.providers_ready:
            await stop.wait()
            return
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(
                total=None, sock_connect=self.config.request_timeout_seconds)) as http:
            self.http = http
            while not stop.is_set() and self.providers_ready:
                self.http = http
                worker = None
                try:
                    async with http.ws_connect(self.config.bridge_url + '/v1/audio',
                        headers=self.headers, heartbeat=5,
                        timeout=aiohttp.ClientWSTimeout(ws_close=3)) as ws:
                        self.ws = ws
                        await ws.send_json({'type': 'ready', 'owner_qq': self.owner_qq, 'ready': True})
                        worker = asyncio.create_task(self.transcribe(on_event))
                        await self.receive_audio(on_event)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    self.status = {'phase': 'unavailable', 'error': '通话 Bridge 连接中断'}
                finally:
                    if worker:
                        worker.cancel()
                        await asyncio.gather(worker, return_exceptions=True)
                    await self.end_session()
                    self.ws = None
                    if self.pending_stop:
                        self.pending_stop.cancel()
                        await asyncio.gather(self.pending_stop, return_exceptions=True)
                        self.pending_stop = None
                    self.http = None
                if not stop.is_set() and self.providers_ready:
                    await asyncio.sleep(2)
            self.http = None

    async def send_call_stream(self, provider, text, context, utterance_id):
        if not self.context_valid(context) or self.http is None:
            raise SendInterrupted('call_ended')
        if self.pending_stop:
            await self.pending_stop
        started = time.monotonic()
        first_ms = None
        synthesis_error = None
        async def upload():
            nonlocal first_ms, synthesis_error
            from contextlib import aclosing
            try:
                async with aclosing(bubble_pcm(provider, text)) as stream:
                    async for chunk in stream:
                        if not self.context_valid(context):
                            raise SendInterrupted('owner_speech' if self.session_valid(context) else 'call_ended')
                        if first_ms is None:
                            first_ms = round((time.monotonic() - started) * 1000)
                            log_event(logger, logging.INFO, 'qq_call_tts_first_audio', channel=self.name,
                                session_id=self.session_id, utterance_id=utterance_id, elapsed_ms=first_ms)
                        yield chunk
            except TTSError as error:
                synthesis_error = error
                raise
        async def request_playback():
            async with self.http.post(self.config.bridge_url + '/v1/playback/stream', data=upload(),
                headers={**self.headers, 'Content-Type': 'application/octet-stream',
                    'X-Call-Session': context['call_session_id'],
                    'X-Call-Generation': str(context['call_generation']),
                    'X-Call-Utterance': utterance_id}, timeout=aiohttp.ClientTimeout(total=160)) as response:
                if response.status != 200:
                    raise SendRejected('Streaming phone playback rejected')
                return await response.json()
        request = asyncio.create_task(request_playback())
        try:
            while not request.done():
                await asyncio.wait({request}, timeout=.05)
                if not self.context_valid(context):
                    raise SendInterrupted('owner_speech' if self.session_valid(context) else 'call_ended')
            result = request.result()
            if synthesis_error is not None:
                raise SendRejected(f'Phone speech synthesis failed: {synthesis_error}')
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            if not self.context_valid(context):
                raise SendInterrupted('owner_speech' if self.session_valid(context) else 'call_ended') from error
            if synthesis_error is not None:
                raise SendRejected(f'Phone speech synthesis failed: {synthesis_error}') from error
            raise SendRejected('Streaming playback uncertain; do not retry') from error
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        log_event(logger, logging.INFO, 'qq_call_playback', channel=self.name,
            session_id=context['call_session_id'], state=result.get('state'), streaming=True,
            elapsed_ms=round((time.monotonic() - started) * 1000), tts_first_audio_ms=first_ms,
            bridge_first_frame_ms=result.get('first_frame_ms'), played_ms=result.get('played_ms', 0),
            recognition_to_first_frame_ms=(round((time.time() - context['recognized_at']) * 1000
                - result.get('elapsed_ms', 0) + result.get('first_frame_ms', 0))
                if context.get('recognized_at') and result.get('first_frame_ms') is not None else None))
        if not self.context_valid(context) or result.get('reason') in {'owner_speech', 'call_ended'}:
            raise SendInterrupted(result.get('reason') or 'call_ended')
        if not result.get('ok'):
            raise SendRejected('Streaming phone playback failed')
        return 'call:' + context['call_session_id'] + ':' + utterance_id

    async def send_call_voice(self, audio, context, utterance_id):
        if not self.context_valid(context) or self.http is None:
            raise SendInterrupted('owner_speech' if self.session_valid(context) else 'call_ended')
        if self.pending_stop:
            await self.pending_stop
        if not self.context_valid(context):
            raise SendInterrupted('owner_speech' if self.session_valid(context) else 'call_ended')
        key = (context['call_session_id'], utterance_id)
        if key in self.sent:
            return self.sent[key]
        started = time.monotonic()
        try:
            async with self.http.post(self.config.bridge_url + '/v1/playback', data=audio.data,
                headers={**self.headers, 'Content-Type': 'application/octet-stream',
                    'X-Call-Session': context['call_session_id'],
                    'X-Call-Generation': str(context['call_generation']),
                    'X-Call-Utterance': utterance_id, 'X-Audio-Format': audio.format},
                timeout=aiohttp.ClientTimeout(total=160)) as response:
                if response.status != 200:
                    raise SendRejected('Phone playback rejected')
                result = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            raise SendRejected('Phone playback uncertain; do not retry') from error
        log_event(logger, logging.INFO, 'qq_call_playback', channel=self.name,
                  session_id=context['call_session_id'], state=result.get('state'),
                  elapsed_ms=round((time.monotonic() - started) * 1000), played_ms=result.get('played_ms', 0))
        if not self.context_valid(context):
            raise SendInterrupted('owner_speech' if self.session_valid(context) else 'call_ended')
        if not result.get('ok'):
            if result.get('reason') in {'owner_speech', 'call_ended'}:
                raise SendInterrupted(result['reason'])
            raise SendRejected('Phone playback failed')
        self.sent[key] = 'call:' + context['call_session_id'] + ':' + utterance_id
        while len(self.sent) > 128:
            self.sent.popitem(last=False)
        return self.sent[key]

    async def send_voice(self, audio):
        # Ordinary outbox must supply the persisted session context explicitly.
        raise SendRejected('Phone playback requires a call-bound delivery context')

    async def send_message(self, payload):
        raise SendRejected('Use reply(mode=voice) for a QQ telephone call')

    async def convert_voice(self, voice):
        return None

    def content_blocks(self, segments):
        return []

    def workflow_variables(self):
        return {}
