"""One owner phone lane using the ordinary Planner and delivery interfaces."""
import asyncio
import contextlib
import logging
import time
import uuid
from collections import OrderedDict

import aiohttp

from ..channel import SendRejected
from ..integrations.contracts.asr import ASRError, AudioInput
from ..models import IncomingMessage
from ..observability.events import log_event
from .audio import Segmenter, wav_bytes

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

    def context_valid(self, context):
        return bool(context and self.session_id and self.status.get('phase') == 'connected'
                    and context.get('call_session_id') == self.session_id
                    and context.get('call_generation') == self.generation)

    def message_current(self, event):
        return self.context_valid(event.delivery_context)

    async def end_session(self):
        old = self.session_id
        self.session_id = ''
        self.generation += 1
        self.segmenter.reset()
        if old and self.interrupt:
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
                started, pcm = self.segmenter.feed(message.data)
                if started:
                    self.generation += 1
                    if self.interrupt:
                        self.interrupt('owner_speech')
                    if self.pending_stop:
                        self.pending_stop.cancel()
                    self.pending_stop = asyncio.create_task(self.stop_playback(self.routing_context()))
                if pcm:
                    item = (dict(self.routing_context()), wav_bytes(pcm))
                    if self.queue.full():
                        self.queue.get_nowait()
                    self.queue.put_nowait(item)
            elif message.type == aiohttp.WSMsgType.ERROR:
                break

    async def transcribe(self, on_event):
        while True:
            context, audio = await self.queue.get()
            if not self.context_valid(context):
                continue
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
                      session_id=context['call_session_id'], elapsed_ms=round((time.monotonic() - started) * 1000))
            if text.strip() and self.context_valid(context):
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

    async def send_call_voice(self, audio, context, utterance_id):
        if not self.context_valid(context) or self.http is None:
            raise SendRejected('Call ended or reply superseded')
        if self.pending_stop:
            await self.pending_stop
        if not self.context_valid(context):
            raise SendRejected('Call ended or reply superseded')
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
        if not result.get('ok') or not self.context_valid(context):
            raise SendRejected('Phone playback interrupted')
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
