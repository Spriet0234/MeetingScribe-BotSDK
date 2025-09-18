
import os
import json
import asyncio
import logging
import random
from typing import Optional, Deque, Tuple, List
from collections import deque

import numpy as np
from aiohttp import web, WSMsgType
from faster_whisper import WhisperModel
import uuid
from datetime import datetime, timezone

DATA_DIR = os.getenv("DATA_DIR", "/data")
WRITE_PARTIALS = os.getenv("WRITE_PARTIALS", "false").lower() == "true"


MODEL_NAME = os.getenv("MODEL", "small.en")
DEVICE = os.getenv("DEVICE", "cpu")            
COMPUTE = os.getenv("COMPUTE", "int8")          
PORT = int(os.getenv("PORT", "8080"))
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")

TARGET_SR = 16000
PARTIAL_SEC_MIN = float(os.getenv("PARTIAL_SEC_MIN", "0.6"))  
PARTIAL_SEC_MAX = float(os.getenv("PARTIAL_SEC_MAX", "1.1"))

SEG_SEC_MIN = float(os.getenv("SEG_SEC_MIN", "18.0"))
SEG_SEC_MAX = float(os.getenv("SEG_SEC_MAX", "22.0"))

RMS_SILENT = float(os.getenv("RMS_SILENT", "0.0015"))
COMMIT_SIL_MS = float(os.getenv("COMMIT_SIL_MS", "1200.0"))
TAIL_KEEP_SEC = float(os.getenv("TAIL_KEEP_SEC", "0.5"))

MAX_BUFFER_SEC = float(os.getenv("MAX_BUFFER_SEC", "180.0"))

GLOBAL_DECODE_CONCURRENCY = int(os.getenv("GLOBAL_DECODE_CONCURRENCY", "4"))

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

logging.info(f"Loading model: {MODEL_NAME} on {DEVICE} ({COMPUTE}) …")
model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE)
logging.info("Model ready.")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def safe_filename(s: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in s)

def open_jsonl_writer(meeting_id: str) -> Tuple[str, "io.TextIOWrapper"]:
    folder = os.path.join(DATA_DIR, "transcripts")
    ensure_dir(folder)
    fname = f"{safe_filename(meeting_id)}_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}.jsonl"
    fpath = os.path.join(folder, fname)
    fp = open(fpath, "a", encoding="utf-8", buffering=1)  # line-buffered
    return fpath, fp

def write_jsonl(fp, payload: dict) -> None:
    fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


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

def total_samples_in(pending: Deque[np.ndarray]) -> int:
    return sum(ch.size for ch in pending)

def pop_left_samples(pending: Deque[np.ndarray], n: int) -> np.ndarray:
    """Remove and return exactly n samples from the left of 'pending'."""
    out: List[np.ndarray] = []
    left = n
    while pending and left > 0:
        ch = pending[0]
        if ch.size <= left:
            out.append(pending.popleft())
            left -= ch.size
        else:
            out.append(ch[:left].copy())
            pending[0] = ch[left:]
            left = 0
    return np.concatenate(out, dtype=np.float32) if out else np.empty((0,), dtype=np.float32)

async def run_decode(
    chunks: List[np.ndarray],
    language: Optional[str],
    partial: bool = False,
) -> Tuple[str, float, float]:
    """
    Decode all 'chunks' concatenated; returns (full_text, t0, t1).
    Aggregates ALL segments to avoid losing earlier words in the window.
    """
    if not chunks:
        return "", 0.0, 0.0

    audio = np.concatenate(chunks, dtype=np.float32) if len(chunks) > 1 else chunks[0]

    segments, _ = model.transcribe(
        audio,
        language=language,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=200),
        beam_size=1 if partial else 1,  
        best_of=1,
        condition_on_previous_text=False, 
        word_timestamps=False,
    )

    texts: List[str] = []
    t0 = None
    t1 = None
    for seg in segments:
        if seg.text:
            texts.append(seg.text.strip())
        if t0 is None:
            t0 = float(seg.start)
        t1 = float(seg.end)

    full_text = " ".join(texts).strip()
    return full_text, float(t0 or 0.0), float(t1 or 0.0)

async def decode_and_send_partial(
    ws: web.WebSocketResponse,
    chunks: List[np.ndarray],
    language: Optional[str],
    base_seconds: float,  
    global_sem: asyncio.Semaphore,
    conn_lock: asyncio.Lock,
    partial_period: float,
) -> None:
    """Quick decode; emit only the incremental suffix."""
    try:
        async with global_sem:        
            async with conn_lock:     
                text, t0, t1 = await run_decode(chunks, language, partial=True)
        text = (text or "").strip()
        if not text:
            return

        committed = getattr(ws, "_committed", "")
        last_partial = getattr(ws, "_last_partial", "")
        baseline = (committed + (" " if committed and last_partial else "") + last_partial).strip()

        if text.startswith(baseline):
            suffix = text[len(baseline):].strip()
        else:
            suffix = text

        if suffix:
            ws._last_partial = (last_partial + (" " if last_partial else "") + suffix).strip()  
            await ws.send_json({
                "type": "partial",
                "t0": t0 + base_seconds,
                "t1": t1 + base_seconds,
                "text": suffix
            })
    except Exception as e:
        logging.warning(f"Partial decode failed: {e}")

async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    if AUTH_TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {AUTH_TOKEN}":
            raise web.HTTPUnauthorized(text="invalid token")

    app: web.Application = request.app
    global_sem: asyncio.Semaphore = app["global_decode_sem"]

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    started = False
    client_sr: Optional[int] = None
    language: Optional[str] = None

    PARTIAL_SEC = random.uniform(PARTIAL_SEC_MIN, PARTIAL_SEC_MAX)
    SEG_SEC = random.uniform(SEG_SEC_MIN, SEG_SEC_MAX)

    pending: Deque[np.ndarray] = deque()
    emitted_samples = 0 

    last_partial_at = 0.0  
    silence_ms = 0.0

    conn_decode_lock = asyncio.Lock()
    ws._committed = ""     
    ws._last_partial = ""  

    meeting_id: str = ""
    jsonl_fp = None
    jsonl_path = ""


    logging.info(f"WS connected (PARTIAL_SEC≈{PARTIAL_SEC:.2f}s, SEG_SEC≈{SEG_SEC:.2f}s)")

    async def emit_final_from_audio(final_audio: np.ndarray):
        """Decode 'final_audio' as a FINAL segment and commit it."""
        nonlocal emitted_samples
        base_seconds = emitted_samples / TARGET_SR
        try:
            async with global_sem:
                async with conn_decode_lock:
                    final_text, t0, t1 = await run_decode([final_audio], language, partial=False)
            if final_text:
                await ws.send_json({
                    "type": "final",
                    "t0": t0 + base_seconds,
                    "t1": t1 + base_seconds,
                    "text": final_text
                })
            ws._committed = (getattr(ws, "_committed", "") + (" " if ws._committed and ws._last_partial else "") + getattr(ws, "_last_partial", "")).strip()  # type: ignore[attr-defined]
            ws._last_partial = ""  
        except Exception as e:
            logging.warning(f"Final decode failed: {e}")

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
                    meeting_id = payload.get("meeting_id") or str(uuid.uuid4())
                    jsonl_path, jsonl_fp = open_jsonl_writer(meeting_id)
                    logging.info(f"Transcript file: {jsonl_path}")

                    write_jsonl(jsonl_fp, {
                        "ts": iso_now(),
                        "type": "meta",
                        "event": "start",
                        "meeting_id": meeting_id,
                        "model": MODEL_NAME,
                        "device": DEVICE,
                        "compute": COMPUTE,
                        "sr": client_sr,
                        "lang": language
                       })

                elif payload.get("type") == "stop":
                    if pending:
                        final_audio = pop_left_samples(pending, total_samples_in(pending))
                        await emit_final_from_audio(final_audio)
                        emitted_samples += final_audio.size
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
                    while seconds_of_audio(total_samples_in(pending), TARGET_SR) > MAX_BUFFER_SEC and pending:
                        pending.popleft()

                    rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
                    if rms < RMS_SILENT:
                        silence_ms += 1000.0 * seconds_of_audio(audio.size, TARGET_SR)
                    else:
                        silence_ms = 0.0

                window_sec = seconds_of_audio(total_samples_in(pending), TARGET_SR)
                if window_sec - last_partial_at >= PARTIAL_SEC and total_samples_in(pending) > 0:
                    last_partial_at = window_sec
                    base_seconds = emitted_samples / TARGET_SR

                    async def safe_partial():
                        await decode_and_send_partial(
                            ws,
                            list(pending),
                            language,
                            base_seconds,
                            global_sem,
                            conn_decode_lock,
                            PARTIAL_SEC,
                        )
                    asyncio.create_task(safe_partial())

                if silence_ms >= COMMIT_SIL_MS and total_samples_in(pending) > 0:
                    final_audio = pop_left_samples(pending, total_samples_in(pending))
                    await emit_final_from_audio(final_audio)
                    emitted_samples += final_audio.size
                    silence_ms = 0.0
                    last_partial_at = 0.0  

                if seconds_of_audio(total_samples_in(pending), TARGET_SR) >= SEG_SEC:
                    seg_samples = int(SEG_SEC * TARGET_SR)
                    segment_audio = pop_left_samples(pending, seg_samples)
                    await emit_final_from_audio(segment_audio)
                    emitted_samples += segment_audio.size
                    last_partial_at = 0.0 


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

async def healthz(_request: web.Request) -> web.Response:
    return web.Response(text="ok", status=200)

def build_app() -> web.Application:
    app = web.Application()
    app["global_decode_sem"] = asyncio.Semaphore(GLOBAL_DECODE_CONCURRENCY)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/healthz", healthz)
    return app

if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=PORT)
