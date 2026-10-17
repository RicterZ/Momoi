"""One owner phone lane using the ordinary Planner and delivery interfaces."""
import asyncio
import contextlib
import logging
import time
import uuid
from collections import OrderedDict

import aiohttp

from ... import SendInterrupted, SendRejected
from ....integrations.contracts.asr import ASRError, AudioInput
from ....integrations.contracts.tts import TTSError
from ....models import IncomingMessage
from ....observability.events import log_event
from ....observability.context import log_context
from .audio import Segmenter, wav_bytes
from .gate import CaptureGate, detect_half_duplex
from .speech import bubble_pcm

logger = logging.getLogger('momoi.qq_call')
# The bridge caps one stream at 150 s wall time and 120 s audio; rotate before either.
STREAM_SEGMENT_SECONDS = 100
STREAM_SEGMENT_BYTES = 48000 * 100
STREAM_RECONNECTS = 3


def audible_end(request_started, result):
    """Monotonic end of audible playback: first frame plus PCM duration, not the stdin flush."""
    first = result.get('first_frame_ms')
    played = result.get('played_ms') or 0
    if first is None:
        return request_started + (result.get('elapsed_ms') or 0) / 1000
    return request_started + (first + played) / 1000


class QQCallChannel:
    # Persistent channel ID; independent of the voice_call package directory.
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
        self.asr_stream = None
        self.asr_frames = 0
        self.asr_compute_ms = 0.0
        self.pending_stop = None
        self.sent = OrderedDict()
        self.half_duplex = False
        self.gate = CaptureGate(getattr(config, 'half_duplex_tail_ms', 400))

    def routing_context(self):
        return {'call_session_id': self.session_id, 'call_generation': self.generation}

    def session_valid(self, context):
        return bool(context and self.session_id and self.status.get('phase') == 'connected'
                    and context.get('call_session_id') == self.session_id)

    def context_valid(self, context):
        return self.session_valid(context) and context.get('call_generation') == self.generation

    def message_current(self, event):
        return self.context_valid(event.delivery_context)

    async def end_session(self, *, reason='call_ended'):
        old = self.session_id
        self.session_id = ''
        self.generation += 1
        self.segmenter.reset()
        self.gate.reset()
        self.half_duplex = False
        self.asr_frames = 0
        self.asr_compute_ms = 0.0
        if self.asr_stream is not None:
            with contextlib.suppress(Exception):
                await self.asr_stream.close()
            self.asr_stream = None
        if old:
            log_event(logger, logging.INFO, 'qq_call_session_ended', channel=self.name,
                      session_id=old, reason=reason)
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
                    log_event(logger, logging.INFO, 'qq_call_session_changed', channel=self.name,
                              previous_session_id=self.session_id, session_id=sid,
                              phase=value.get('phase'), ready=value.get('ready'),
                              dependencies=value.get('dependencies'), error=value.get('error'))
                    await self.end_session(reason='session_replaced' if sid else 'bridge_session_ended')
                    self.session_id = sid
                    self.generation = int(value.get('generation') or 0)
                    if sid:
                        self.half_duplex, reason = detect_half_duplex(
                            value.get('audio_devices'), mode=getattr(self.config, 'half_duplex', 'auto'),
                            bridge_flag=bool(value.get('half_duplex')))
                        log_event(logger, logging.INFO, 'qq_call_half_duplex', channel=self.name,
                                  session_id=sid, half_duplex=self.half_duplex, reason=reason)
                self.status = {k: value[k] for k in ('phase', 'ready', 'error', 'dependencies') if k in value}
            elif message.type == aiohttp.WSMsgType.BINARY and self.session_id:
                if self.half_duplex and not await self.admit_frame(message.data):
                    continue
                if callable(getattr(self.asr, 'create_stream', None)):
                    try:
                        if self.asr_stream is None:
                            self.asr_stream = await self.asr.create_stream()
                            log_event(logger, logging.DEBUG, 'qq_call_local_asr_stream_started',
                                      session_id=self.session_id, provider=type(self.asr).__name__)
                        started = time.monotonic()
                        result = await self.asr_stream.feed(message.data)
                        self.asr_frames += 1
                        self.asr_compute_ms += (time.monotonic() - started) * 1000
                    except (ASRError, ValueError, RuntimeError) as error:
                        log_event(logger, logging.ERROR, 'qq_call_local_asr_failed',
                                  session_id=self.session_id, error_type=type(error).__name__)
                        self.status = {'phase': 'error', 'error': '本地 ASR 不可用，请检查组件或容器'}
                        await self.ws.close()
                        return
                    if result['final']:
                        log_event(logger, logging.DEBUG, 'qq_call_local_asr_endpoint',
                                  session_id=self.session_id, frames=self.asr_frames,
                                  compute_ms=round(self.asr_compute_ms), recognized=bool(result['text']),
                                  trailing_silence_ms=round(getattr(self.asr, 'trailing_silence', 0) * 1000))
                        self.asr_frames = 0
                        self.asr_compute_ms = 0.0
                    if result['final'] and result['text']:
                        context = {**self.routing_context(), 'segment_finished_at': time.time()}
                        if self.queue.full():
                            self.queue.get_nowait()
                        self.queue.put_nowait((context, result['text']))
                    continue
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

    async def admit_frame(self, frame):
        if self.gate.poll():
            self.segmenter.reset()
            self.asr_frames = 0
            self.asr_compute_ms = 0.0
            if self.asr_stream is not None:
                stream, self.asr_stream = self.asr_stream, None
                with contextlib.suppress(Exception):
                    await stream.close()
        if not self.gate.closed:
            return True
        import array
        samples = array.array('h', frame[:len(frame) // 2 * 2])
        self.gate.drop(max((abs(value) for value in samples), default=0))
        return False

    def capture_turn_started(self, turn_id):
        if self.half_duplex:
            self.gate.turn_started(turn_id)

    def capture_turn_finished(self, turn_id, *, cancelled=False):
        if self.half_duplex:
            self.gate.turn_finished(turn_id, cancelled=cancelled)

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
                text = audio if isinstance(audio, str) else await self.asr.transcribe(AudioInput(audio, 'wav'))
                if text == '嗯。':
                    text = ''
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
            if text.strip() and self.context_valid(context) and self.half_duplex:
                if self.gate.closed:
                    log_event(logger, logging.INFO, 'qq_call_speech_discarded', channel=self.name,
                              session_id=self.session_id, reason='capture_gate_closed')
                    continue
                context = {**context, 'recognized_at': time.time()}
                event_id = 'qq-call:' + context['call_session_id'] + ':' + uuid.uuid4().hex
                now = time.time()
                await on_event(IncomingMessage(event_id, event_id, text.strip(), now, now,
                                               channel=self.name, delivery_context=context))
            elif text.strip() and self.context_valid(context):
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
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                    log_event(logger, logging.WARNING, 'qq_call_bridge_connection_failed',
                              channel=self.name, session_id=self.session_id,
                              error_type=type(error).__name__)
                    self.status = {'phase': 'unavailable', 'error': '通话 Bridge 连接中断'}
                finally:
                    if worker:
                        worker.cancel()
                        await asyncio.gather(worker, return_exceptions=True)
                    await self.end_session(reason='channel_stopped' if stop.is_set() else 'bridge_disconnected')
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
        if self.half_duplex:
            self.gate.send_started(utterance_id)
            end = None
            try:
                result = await self._send_call_stream(provider, text, context, utterance_id)
                end = self._audible_end
                return result
            finally:
                self.gate.send_finished(audible_end=end)
        return await self._send_call_stream(provider, text, context, utterance_id)

    async def _send_call_stream(self, provider, text, context, utterance_id):
        self._audible_end = None
        started = time.monotonic()
        first_ms = None
        synthesis_error = None
        source_error = None
        pcm_bytes = 0
        pcm_peak = 0
        first_signal = False
        chunks = asyncio.Queue(maxsize=64)
        finished = object()
        carry = None
        log_event(logger, logging.DEBUG, 'qq_call_send_started', session_id=self.session_id,
                  utterance_id=utterance_id, provider=type(provider).__name__, text_chars=len(text))

        def interrupted():
            return SendInterrupted('owner_speech' if self.session_valid(context) else 'call_ended')

        # Synthesis outlives any single bridge request, so a reconnect resumes the same speech.
        async def pump():
            nonlocal synthesis_error, source_error
            from contextlib import aclosing
            try:
                with log_context(session_id=context['call_session_id'], utterance_id=utterance_id):
                    async with aclosing(bubble_pcm(provider, text, pause_seconds=.5, paced=True)) as stream:
                        async for chunk in stream:
                            await chunks.put(chunk)
            except TTSError as error:
                synthesis_error = error
            except Exception as error:
                source_error = error
            await chunks.put(finished)

        async def next_chunk(timeout=None):
            getter = asyncio.ensure_future(chunks.get())
            deadline = None if timeout is None else time.monotonic() + timeout
            try:
                while not getter.done():
                    wait = .05 if deadline is None else min(.05, max(0, deadline - time.monotonic()))
                    await asyncio.wait({getter}, timeout=wait)
                    if not self.context_valid(context):
                        raise interrupted()
                    if deadline is not None and time.monotonic() >= deadline and not getter.done():
                        return None
                return getter.result()
            finally:
                if not getter.done():
                    getter.cancel()
                    await asyncio.gather(getter, return_exceptions=True)
                    # A cancelled get may already have dequeued its item.
                    if not getter.cancelled() and getter.exception() is None:
                        chunks.put_nowait(getter.result())

        async def segment(first):
            nonlocal first_ms, pcm_bytes, pcm_peak, first_signal, carry
            import array
            deadline = time.monotonic() + STREAM_SEGMENT_SECONDS
            sent = 0
            item = first
            while True:
                if not self.context_valid(context):
                    raise interrupted()
                if first_ms is None:
                    first_ms = round((time.monotonic() - started) * 1000)
                    log_event(logger, logging.INFO, 'qq_call_tts_first_audio', channel=self.name,
                        session_id=self.session_id, utterance_id=utterance_id, elapsed_ms=first_ms)
                pcm_bytes += len(item)
                samples = array.array('h', item[:len(item)//2*2])
                pcm_peak = max(pcm_peak, max((abs(value) for value in samples), default=0))
                if not first_signal and pcm_peak > 32:
                    first_signal = True
                    log_event(logger, logging.INFO, 'qq_call_tts_first_signal',
                              elapsed_ms=round((time.monotonic() - started) * 1000),
                              audio_offset_ms=round((pcm_bytes - len(item)) / 48))
                yield item
                sent += len(item)
                remaining = deadline - time.monotonic()
                if sent >= STREAM_SEGMENT_BYTES or remaining <= 0:
                    return
                item = await next_chunk(remaining)
                if item is None:
                    return
                if item is finished:
                    carry = finished
                    return

        async def request_playback(first, part_id):
            async with self.http.post(self.config.bridge_url + '/v1/playback/stream', data=segment(first),
                headers={**self.headers, 'Content-Type': 'application/octet-stream',
                    'X-Call-Session': context['call_session_id'],
                    'X-Call-Generation': str(context['call_generation']),
                    'X-Call-Utterance': part_id}, timeout=aiohttp.ClientTimeout(total=160)) as response:
                log_event(logger, logging.DEBUG, 'qq_call_bridge_response', session_id=self.session_id,
                          utterance_id=part_id, status=response.status, pcm_bytes=pcm_bytes, peak=pcm_peak)
                if response.status != 200:
                    raise SendRejected('Streaming phone playback rejected')
                return await response.json()

        async def play(first, part_id):
            request_started = time.monotonic()
            request = asyncio.create_task(request_playback(first, part_id))
            try:
                while not request.done():
                    await asyncio.wait({request}, timeout=.05)
                    if not self.context_valid(context):
                        raise interrupted()
                result = request.result()
                if result.get('ok'):
                    self._audible_end = max(self._audible_end or 0, audible_end(request_started, result))
                return result
            finally:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)

        producer = asyncio.create_task(pump())
        results = []
        failures = 0
        part = 0
        try:
            while True:
                item, carry = (carry, None) if carry is not None else (await next_chunk(), None)
                if item is finished:
                    break
                part_id = utterance_id if part == 0 else f'{utterance_id}#{part}'
                part += 1
                try:
                    result = await play(item, part_id)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                    if not self.context_valid(context):
                        raise interrupted() from error
                    result, failure = None, error
                if result is not None:
                    if not self.context_valid(context) or result.get('reason') in {'owner_speech', 'call_ended'}:
                        raise SendInterrupted(result.get('reason') or 'call_ended')
                    if result.get('ok'):
                        results.append(result)
                        failures = 0
                        continue
                    failure = None
                failures += 1
                if failures > STREAM_RECONNECTS:
                    if synthesis_error is not None:
                        raise SendRejected(f'Phone speech synthesis failed: {synthesis_error}') from failure
                    if failure is not None:
                        raise SendRejected('Streaming playback uncertain; do not retry') from failure
                    raise SendRejected('Streaming phone playback failed')
                log_event(logger, logging.WARNING, 'qq_call_playback_reconnect', channel=self.name,
                          session_id=context['call_session_id'], utterance_id=part_id, attempt=failures,
                          error_type=type(failure).__name__ if failure is not None else 'bridge_not_ok')
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        if synthesis_error is not None and not results:
            raise SendRejected(f'Phone speech synthesis failed: {synthesis_error}')
        if source_error is not None and not results:
            raise SendRejected('Streaming phone audio failed') from source_error
        if not results:
            raise SendRejected('Streaming phone playback failed')
        if self._audible_end is None:
            self._audible_end = time.monotonic()
        log_event(logger, logging.DEBUG, 'qq_call_pcm_uploaded', session_id=self.session_id,
                  utterance_id=utterance_id, pcm_bytes=pcm_bytes, peak=pcm_peak, segments=part)
        result = results[0]
        log_event(logger, logging.INFO, 'qq_call_playback', channel=self.name,
            session_id=context['call_session_id'], utterance_id=utterance_id,
            state=results[-1].get('state'), streaming=True, segments=part,
            elapsed_ms=round((time.monotonic() - started) * 1000), tts_first_audio_ms=first_ms,
            bridge_first_frame_ms=result.get('first_frame_ms'),
            played_ms=sum(item.get('played_ms', 0) for item in results),
            virtual_mic_signal_ms=result.get('virtual_mic_signal_ms'),
            measurement_endpoint='playback_stdin_written', remote_audible_at=None,
            first_frame_to_signal_ms=(result['virtual_mic_signal_ms'] - result['first_frame_ms']
                if result.get('virtual_mic_signal_ms') is not None and result.get('first_frame_ms') is not None else None),
            recognition_to_first_frame_ms=(round((time.time() - context['recognized_at']) * 1000
                - result.get('elapsed_ms', 0) + result.get('first_frame_ms', 0))
                if context.get('recognized_at') and result.get('first_frame_ms') is not None else None))
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
        if not self.half_duplex:
            return await self._send_call_voice(audio, context, utterance_id, key)
        self.gate.send_started(utterance_id)
        self._audible_end = None
        end = None
        try:
            result = await self._send_call_voice(audio, context, utterance_id, key)
            end = self._audible_end
            return result
        finally:
            self.gate.send_finished(audible_end=end)

    async def _send_call_voice(self, audio, context, utterance_id, key):
        self._audible_end = None
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
        self._audible_end = audible_end(started, result)
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
