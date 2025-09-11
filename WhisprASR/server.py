import os
import json
import asyncio
import logging
from typing import Optional, Deque, Tuple
from collections import deque

import numpy as np
from aiohttp import web, WSMsgType
from faster_whisper import WhisperModel

MODEL_NAME = os.getenv("MODEL", "small.en")
DEVICE = os.getenv("DEVICE", "cpu")           
COMPUTE = os.getenv("COMPUTE", "int8")       
PORT = int(os.getenv("PORT", "8080"))
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")       
TARGET_SR = 16000                            
PARTIAL_SEC = 1.0                              

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

logging.info(f"Loading model: {MODEL_NAME} on {DEVICE} ({COMPUTE}) …")
model = WhisperModel(
    MODEL_NAME,
    device=DEVICE,
    compute_type=COMPUTE,     
)

model_ready = True
logging.info("Model ready.")

def pcm16le_bytes_to_float32(buf: bytes) -> np.ndarray:
    """Convert raw little-endian signed 16-bit PCM to float32 [-1,1]."""
    if not buf:
        return np.empty((0,), dtype=np.float32)
    arr = np.frombuffer(buf, dtype=np.int16).astype(np.float32)
    return arr / 32768.0

def simple_resample_linear(x: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    """Very simple linear resampler. For production, use libsoxr/FFmpeg."""
    if sr_from == sr_to or x.size == 0:
        return x
    ratio = sr_to / sr_from
    new_len = int(np.floor(x.size * ratio))
    xp = np.linspace(0, 1, num=x.size, endpoint=False)
    xq = np.linspace(0, 1, num=new_len, endpoint=False)
    return np.interp(xq, xp, x).astype(np.float32)

def seconds_of_audio(num_samples: int, sr: int) -> float:
    return num_samples / float(sr)

async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    if AUTH_TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {AUTH_TOKEN}":
            raise web.HTTPUnauthorized(text="invalid token")

    ws = web.WebSocketResponse(heartbeat=30)  
    await ws.prepare(request)

    started = False
    client_sr: Optional[int] = None
    language: Optional[str] = None

    pending: Deque[np.ndarray] = deque()
    decoded_samples = 0  

    last_decode_at = 0.0  

    logging.info("WS connected")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "error": "invalid_json"})
                    continue

                if payload.get("type") == "start":
                    if started:
                        await ws.send_json({"type": "error", "error": "already_started"})
                        continue
                    client_sr = int(payload.get("sample_rate", TARGET_SR))
                    language = payload.get("language", "en")
                    started = True
                    await ws.send_json({"type": "ack", "message": "started"})
                    logging.info(f"Stream started: sr={client_sr}, lang={language}")

                elif payload.get("type") == "stop":
                    final_text, t0, t1 = await run_decode(list(pending), decoded_samples, language)
                    if final_text:
                        await ws.send_json({"type": "final", "t0": t0, "t1": t1, "text": final_text})
                    await ws.close()
                    logging.info("WS closed (stop)")
                    break

                else:
                    await ws.send_json({"type": "error", "error": "unknown_control_message"})

            elif msg.type == WSMsgType.BINARY:
                if not started:
                    await ws.send_json({"type": "error", "error": "send_start_first"})
                    continue

                audio = pcm16le_bytes_to_float32(msg.data)
                if client_sr and client_sr != TARGET_SR:
                    audio = simple_resample_linear(audio, client_sr, TARGET_SR)

                if audio.size:
                    pending.append(audio)

                total_samples = sum(ch.size for ch in pending)
                now_sec = seconds_of_audio(total_samples, TARGET_SR)
                if now_sec - last_decode_at >= PARTIAL_SEC:
                    asyncio.create_task(decode_and_send_partial(ws, list(pending), decoded_samples, language))
                    last_decode_at = now_sec

            elif msg.type == WSMsgType.ERROR:
                logging.warning(f"WS error: {ws.exception()}")
                break

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.exception(f"WS handler exception: {e}")
    finally:
        await ws.close()
        logging.info("WS disconnected")

    return ws

async def decode_and_send_partial(
    ws: web.WebSocketResponse,
    chunks: list[np.ndarray],
    offset_samples: int,
    language: Optional[str],
) -> None:
    """Runs a quick decode over the buffered audio and emits a 'partial'."""
    try:
        text, t0, t1 = await run_decode(chunks, offset_samples, language, partial=True)
        if text:
            await ws.send_json({"type": "partial", "t0": t0, "t1": t1, "text": text})
    except Exception as e:
        logging.warning(f"Partial decode failed: {e}")

async def run_decode(
    chunks: list[np.ndarray],
    offset_samples: int,
    language: Optional[str],
    partial: bool = False,
) -> Tuple[str, float, float]:
    """
    Concatenate audio and run faster-whisper. We return the last segment's text
    as the partial; on final, same logic (simple but effective for MVP).
    """
    if not chunks:
        return "", 0.0, 0.0

    audio = np.concatenate(chunks, dtype=np.float32) if len(chunks) > 1 else chunks[0]
    segments, _ = model.transcribe(
        audio,
        language=language,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=200),
        beam_size=1 if partial else 5,  
        condition_on_previous_text=True,
        word_timestamps=False,
    )

    last_text = ""
    last_t0 = 0.0
    last_t1 = 0.0
    for seg in segments:
        last_text = (seg.text or "").strip()
        last_t0 = float(seg.start)
        last_t1 = float(seg.end)

    return last_text, last_t0, last_t1

async def healthz(_request: web.Request) -> web.Response:
    if model_ready:
        return web.Response(text="ok", status=200)
    return web.Response(text="loading", status=503)

def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/healthz", healthz)
    return app

if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=PORT)
