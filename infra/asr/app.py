"""Private CPU ASR service: isolated stream state for each WebSocket."""
import asyncio
from contextlib import asynccontextmanager
import os
import wave

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from momoi.integrations.adapters.sherpa import SherpaEngine

engine = None
slots = asyncio.Semaphore(4)


@asynccontextmanager
async def lifespan(app):
    global engine
    engine = await asyncio.to_thread(SherpaEngine, os.environ.get('ASR_MODEL_PATH', '/models'),
                                    int(os.environ.get('ASR_NUM_THREADS', '2')),
                                    float(os.environ.get('ASR_TRAILING_SILENCE', '1.2')))
    yield


app = FastAPI(lifespan=lifespan)


@app.get('/healthz')
def health():
    return {'ready': engine is not None, 'engine': 'zipformer-zh-int8-2025-06-30'}


@app.post('/v1/transcribe')
async def transcribe(request: Request):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 3 * 1024 * 1024:
            raise HTTPException(413, 'Audio too large')
    async with slots:
        try:
            text = await asyncio.to_thread(engine.transcribe, bytes(data))
            return {'text': text}
        except (ValueError, EOFError, wave.Error) as error:
            raise HTTPException(400, str(error)) from error


@app.websocket('/v1/stream')
async def stream(ws: WebSocket):
    await ws.accept()
    try:
        await asyncio.wait_for(slots.acquire(), timeout=1)
    except asyncio.TimeoutError:
        await ws.close(code=1013)
        return
    try:
        state = await asyncio.to_thread(engine.create_stream)
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
            result = await asyncio.to_thread(engine.feed, state, pcm, finish=finish)
            await ws.send_json(result)
            if finish:
                break
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        slots.release()
