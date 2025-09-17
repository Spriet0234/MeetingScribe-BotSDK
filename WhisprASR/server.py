import os
import json
import asyncio
import logging
from typing import Optional, Deque, Tuple
from collections import deque

import numpy as np
from aiohttp import web, WSMsgType
from faster_whisper import WhisperModel

# ───────── Config ─────────
MODEL_NAME = os.getenv("MODEL", "small.en")
DEVICE = os.getenv("DEVICE", "cpu")
COMPUTE = os.getenv("COMPUTE", "int8")
PORT = int(os.getenv("PORT", "8080"))
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")

TARGET_SR = 16000
PARTIAL_SEC = 1.0  # try 0.6–1.0s if you want faster partials

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

# ───────── Model ─────────
logging.info(f"Loading model: {MODEL_NAME} on {DEVICE} ({COMPUTE}) …")
model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE)
model_ready = True
logging.info("Model ready.")

# ───────── Helpers ─────────
def pcm16le_bytes_to_float32(buf: bytes) -> np.ndarray:
    if not buf:
        return np.empty((0,), dtype=np.float32)
    arr = np.frombuffer(buf, dtype=np.int16).astype(np.float32)
    return arr / 32768.0

def simple_resample_linear(x: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    if sr_from == sr_to or x.size == 0:
        return x
    ratio = sr_to / sr_from
    new_len = int(np.floor(x.size * ratio))
    xp = np.linspace(0, 1, num=x.size, endpoint=False)
    xq = np.linspace(0, 1, num=new_len, endpoint=False)
    return np.interp(xq, xp, x).astype(np.float32)

def seconds_of_audio(num_samples: int, sr: int) -> float:
    return num_samples / float(sr)

# ───────── WS Handler ─────────
async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    # Auth (optional)
    if AUTH_TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {AUTH_TOKEN}":
            raise web.HTTPUnauthorized(text="invalid token")

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    # Per-connection state
    started = False
    client_sr: Optional[int] = None
    language: Optional[str] = None

    # Audio buffer at TARGET_SR
    pending: Deque[np.ndarray] = deque()
    decoded_samples = 0

    last_decode_at = 0.0

    # NEW: ensure only one decode runs at a time
    decode_lock = asyncio.Lock()

    # NEW: dedupe partials
    ws._last_partial = ""  # type: ignore[attr-defined]

    # NEW: cap rolling buffer (seconds)
    MAX_SEC = 20.0

    # NEW: commit-on-silence knobs (enabled)
    RMS_SILENT = 0.002      # tweak per mic level
    COMMIT_SIL_MS = 800.0   # emit final after ~0.8s silence
    TAIL_KEEP_SEC = 0.5     # keep small tail after final for context
    silence_ms = 0.0

    logging.info("WS connected")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # control messages
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
                    # final decode on remaining audio
                    async with decode_lock:
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

                # bytes -> float -> resample if needed
                audio = pcm16le_bytes_to_float32(msg.data)
                if client_sr and client_sr != TARGET_SR:
                    audio = simple_resample_linear(audio, client_sr, TARGET_SR)

                if audio.size:
                    pending.append(audio)

                    # NEW: cap to last MAX_SEC of audio
                    while seconds_of_audio(sum(ch.size for ch in pending), TARGET_SR) > MAX_SEC:
                        pending.popleft()

                    # NEW: crude RMS-based silence tracking
                    rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
                    if rms < RMS_SILENT:
                        silence_ms += 1000.0 * seconds_of_audio(audio.size, TARGET_SR)
                    else:
                        silence_ms = 0.0

                # trigger partial periodically, but serialize with a lock
                total_samples = sum(ch.size for ch in pending)
                now_sec = seconds_of_audio(total_samples, TARGET_SR)
                if now_sec - last_decode_at >= PARTIAL_SEC:
                    last_decode_at = now_sec

                    async def safe_partial():
                        async with decode_lock:
                            await decode_and_send_partial(ws, list(pending), decoded_samples, language)

                    asyncio.create_task(safe_partial())

                # NEW: commit-on-silence → emit final and trim old audio
                if silence_ms >= COMMIT_SIL_MS and total_samples > 0:
                    async def commit_final():
                        async with decode_lock:
                            final_text, t0, t1 = await run_decode(list(pending), decoded_samples, language, partial=False)
                            if final_text:
                                await ws.send_json({"type": "final", "t0": t0, "t1": t1, "text": final_text})

                            # Trim buffer but keep a short tail for acoustic context
                            tail_keep = int(TAIL_KEEP_SEC * TARGET_SR)
                            drop = max(0, total_samples - tail_keep)
                            dropped = 0
                            while pending and dropped + pending[0].size <= drop:
                                dropped += pending.popleft().size

                            # reset last partial so the next phrase appears
                            ws._last_partial = ""  # type: ignore[attr-defined]

                    asyncio.create_task(commit_final())
                    silence_ms = 0.0

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

# ───────── Decode helpers ─────────
async def decode_and_send_partial(
    ws: web.WebSocketResponse,
    chunks: list[np.ndarray],
    offset_samples: int,
    language: Optional[str],
) -> None:
    """Quick decode; only emit when text actually changes."""
    try:
        text, t0, t1 = await run_decode(chunks, offset_samples, language, partial=True)
        text = (text or "").strip()
        if text and text != getattr(ws, "_last_partial", ""):
            ws._last_partial = text  # type: ignore[attr-defined]
            await ws.send_json({"type": "partial", "t0": t0, "t1": t1, "text": text})
    except Exception as e:
        logging.warning(f"Partial decode failed: {e}")

async def run_decode(
    chunks: list[np.ndarray],
    offset_samples: int,
    language: Optional[str],
    partial: bool = False,
) -> Tuple[str, float, float]:
    if not chunks:
        return "", 0.0, 0.0

    audio = np.concatenate(chunks, dtype=np.float32) if len(chunks) > 1 else chunks[0]
    segments, _ = model.transcribe(
        audio,
        language=language,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=200),
        beam_size=1 if partial else 5,  # faster partials, better finals
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

# ───────── Health & App ─────────
async def healthz(_request: web.Request) -> web.Response:
    return web.Response(text="ok" if model_ready else "loading", status=200 if model_ready else 503)

def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/healthz", healthz)
    return app

if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=PORT)
