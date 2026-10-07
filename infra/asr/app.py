"""Private CPU ASR service: isolated stream state for each WebSocket."""
import asyncio
from contextlib import asynccontextmanager
import wave

from fastapi import FastAPI, HTTPException, Request, Query, WebSocket, WebSocketDisconnect
from momoi.integrations.adapters.sherpa import SherpaEngine

engine = None
slots = asyncio.Semaphore(4)
engine_settings = None
engine_lock = asyncio.Lock()


async def configured_engine(num_threads=2, trailing_silence=0.8):
    """Reuse the configured model; active streams retain their own engine."""
    global engine, engine_settings
    settings = (num_threads, trailing_silence)
    async with engine_lock:
        if engine is None or engine_settings != settings:
            engine = await asyncio.to_thread(SherpaEngine, '/models', *settings)
            engine_settings = settings
        return engine


@asynccontextmanager
async def lifespan(app):
    await configured_engine()
    yield


app = FastAPI(lifespan=lifespan)


@app.get('/healthz')
def health():
    return {'ready': engine is not None, 'engine': 'zipformer-zh-int8-2025-06-30',
            'num_threads': engine_settings[0] if engine_settings else None,
            'trailing_silence': engine_settings[1] if engine_settings else None}


@app.post('/v1/transcribe')
async def transcribe(request: Request, num_threads: int = Query(2, ge=1, le=16),
                     trailing_silence: float = Query(0.8, gt=0, le=5)):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 3 * 1024 * 1024:
            raise HTTPException(413, 'Audio too large')
    async with slots:
        try:
            active_engine = await configured_engine(num_threads, trailing_silence)
            text = await asyncio.to_thread(active_engine.transcribe, bytes(data))
            return {'text': text}
        except (ValueError, EOFError, wave.Error) as error:
            raise HTTPException(400, str(error)) from error


@app.websocket('/v1/stream')
async def stream(ws: WebSocket, num_threads: int = Query(2, ge=1, le=16),
                 trailing_silence: float = Query(0.8, gt=0, le=5)):
    await ws.accept()
    try:
        await asyncio.wait_for(slots.acquire(), timeout=1)
    except asyncio.TimeoutError:
        await ws.close(code=1013)
        return
    try:
        active_engine = await configured_engine(num_threads, trailing_silence)
        state = await asyncio.to_thread(active_engine.create_stream)
        while True:
            message = await asyncio.wait_for(ws.receive(), timeout=60)
            if message['type'] == 'websocket.disconnect':
                break
            pcm = message.get('bytes')
            finish = False
            if pcm is None:
                if message.get('text') != 'finish':
                    await ws.close(code=1003)
                    break
                pcm, finish = b'', True
            if len(pcm) > 32000 or len(pcm) % 2:
                await ws.close(code=1009)
                break
            result = await asyncio.to_thread(active_engine.feed, state, pcm, finish=finish)
            await ws.send_json(result)
            if finish:
                break
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        slots.release()
