#!/usr/bin/env python3
import os
import gc
import json
import time
import asyncio
import logging
from typing import Optional, Deque, Tuple, Dict, List
from collections import deque

import numpy as np
from aiohttp import web, WSMsgType
from faster_whisper import WhisperModel
import aiohttp
import boto3

# ──────────────────────────────────────────────────────────────────────────────
# Environment / Config
# ──────────────────────────────────────────────────────────────────────────────
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_PREFIX_TRANSCRIPTS = os.getenv("S3_PREFIX_TRANSCRIPTS", "transcripts")
SUMMARIZER_URL = os.getenv("SUMMARIZER_URL", "http://summarizer:9001/jobs")

MODEL_NAME = os.getenv("MODEL", "small.en")
DEVICE = os.getenv("DEVICE", "cpu")            # "cpu" | "cuda"
COMPUTE = os.getenv("COMPUTE", "int8")         # "int8", "float16", etc.
PORT = int(os.getenv("PORT", "8080"))
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")

TARGET_SR = 16000
PARTIAL_SEC = float(os.getenv("PARTIAL_SEC", "1.0"))   # seconds between partial decodes

OUTPUT_DIR = os.getenv("DATA_DIR", "/data")
TRANSCRIPT_DIR = os.path.join(OUTPUT_DIR, "transcripts")
os.makedirs(TRANSCRIPT_DIR, exist_ok=True)

# Silence / commit tuning (safe defaults for meetings)
RMS_SILENT       = float(os.getenv("RMS_SILENT", "0.002"))
COMMIT_SIL_MS    = float(os.getenv("COMMIT_SIL_MS", "700.0"))
TAIL_KEEP_SEC    = float(os.getenv("TAIL_KEEP_SEC", "0.75"))
MAX_SEC          = float(os.getenv("MAX_SEC", "18.0"))      # global rolling cap
MAX_UTTER_SEC    = float(os.getenv("MAX_UTTER_SEC", "8.0")) # force final if too long

# Performance / stability knobs
CPU_THREADS           = int(os.getenv("CPU_THREADS", "2"))
BEAM_PARTIAL          = int(os.getenv("BEAM_PARTIAL", "1"))
BEAM_FINAL            = int(os.getenv("BEAM_FINAL", "1"))
COND_PREV             = os.getenv("CONDITION_ON_PREVIOUS", "false").lower() == "true"
NO_PROGRESS_SEC       = float(os.getenv("NO_PROGRESS_SEC", "8.0"))
PARTIAL_WINDOW_SEC    = float(os.getenv("PARTIAL_WINDOW_SEC", "6.0"))
MIN_PARTIAL_DELTA_SEC = float(os.getenv("MIN_PARTIAL_DELTA_SEC", "0.4"))
REPEAT_GUARD_N        = int(os.getenv("REPEAT_GUARD_N", "2"))

# Backpressure hard limit multiplier: if pending grows beyond this * MAX_SEC, trim
BACKPRESSURE_X = float(os.getenv("BACKPRESSURE_X", "1.5"))

# Lightweight GC to keep RSS stable on long runs
GC_EVERY_SEC = float(os.getenv("GC_EVERY_SEC", "20.0"))  # 0 to disable

# Logging
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

# Threads for ctranslate2 / OpenMP
os.environ.setdefault("CT2_NUM_THREADS", str(CPU_THREADS))
os.environ.setdefault("OMP_NUM_THREADS",  str(CPU_THREADS))

# ──────────────────────────────────────────────────────────────────────────────
# Model load
# ──────────────────────────────────────────────────────────────────────────────
logging.info(
    f"Loading model: {MODEL_NAME} on {DEVICE} ({COMPUTE}) … "
    f"threads={CPU_THREADS}, cond_prev={COND_PREV}"
)
model = WhisperModel(
    MODEL_NAME,
    device=DEVICE,
    compute_type=COMPUTE,
    cpu_threads=CPU_THREADS
)
model_ready = True
logging.info("Model ready.")

# ──────────────────────────────────────────────────────────────────────────────
# Speaker state via UDP from bot
# ──────────────────────────────────────────────────────────────────────────────
current_active_ids: List[str] = []
name_map: Dict[str, str] = {}

class _UdpProto(asyncio.DatagramProtocol):
    def __init__(self, handler):
        self.handler = handler
    def datagram_received(self, data, addr):
        try:
            self.handler(data.decode("utf-8", errors="ignore").strip())
        except Exception:
            logging.exception("UDP parse failed")

def _handle_active(msg: str):
    # Expect: "active=uid,uid,uid"
    if msg.startswith("active="):
        ids = [s for s in msg[7:].split(",") if s]
        global current_active_ids
        current_active_ids = ids

def _handle_map(msg: str):
    # Expect: "map=uid:name|uid:name|..."
    if msg.startswith("map="):
        part = msg[4:]
        for it in part.split("|"):
            if not it:
                continue
            uid, _, name = it.partition(":")
            uid = uid.strip()
            name = (name or "User").strip()
            if uid:
                name_map[uid] = name

def current_speaker_label() -> Tuple[str, str]:
    uid = current_active_ids[0] if current_active_ids else ""
    return (uid, name_map.get(uid, f"User {uid}") if uid else "Unknown")

async def _start_udp(app: web.Application):
    loop = asyncio.get_running_loop()
    t1, _ = await loop.create_datagram_endpoint(
        lambda: _UdpProto(_handle_active),
        local_addr=("127.0.0.1", 7100),
    )
    t2, _ = await loop.create_datagram_endpoint(
        lambda: _UdpProto(_handle_map),
        local_addr=("127.0.0.1", 7101),
    )
    app["udp_transports"] = [t1, t2]
    logging.info("UDP listeners up on 127.0.0.1:7100 (active), :7101 (map)")

async def _stop_udp(app: web.Application):
    for t in app.get("udp_transports", []):
        t.close()

# ──────────────────────────────────────────────────────────────────────────────
# S3 / Summarizer
# ──────────────────────────────────────────────────────────────────────────────
_s3 = None
def _s3_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=AWS_REGION)
    return _s3

def _s3_key_for_session(session_id: str) -> str:
    return f"{S3_PREFIX_TRANSCRIPTS.rstrip('/')}/{session_id}.jsonl"

def _presigned_get_for_session(session_id: str, expires=3600) -> str:
    key = _s3_key_for_session(session_id)
    return _s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=expires,
    )

def _upload_transcript_jsonl_to_s3(local_path: str, session_id: str) -> str:
    key = _s3_key_for_session(session_id)
    logging.info(f"[s3] put_object -> s3://{S3_BUCKET}/{key}")
    with open(local_path, "rb") as f:
        _s3_client().put_object(Bucket=S3_BUCKET, Key=key, Body=f,
                                ContentType="application/x-ndjson")
    logging.info(f"[s3] uploaded: s3://{S3_BUCKET}/{key}")
    return f"s3://{S3_BUCKET}/{key}"

async def enqueue_summary_job(session_id: str, transcript_jsonl_abs: str, model_name: Optional[str] = None):
    payload = {"session_id": session_id, "transcript_url": transcript_jsonl_abs}
    if model_name:
        payload["model"] = model_name
    async with aiohttp.ClientSession() as http:
        async with http.post(SUMMARIZER_URL, json=payload) as resp:
            if resp.status >= 300:
                txt = await resp.text()
                raise RuntimeError(f"enqueue failed: {resp.status} {txt}")
            return await resp.json()

# ──────────────────────────────────────────────────────────────────────────────
# IO helpers
# ──────────────────────────────────────────────────────────────────────────────
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

# ──────────────────────────────────────────────────────────────────────────────
# Transcript recorder (JSONL)
# ──────────────────────────────────────────────────────────────────────────────
class TranscriptRecorderJSONL:
    """Writes only FINALS to JSONL; partials are not persisted."""
    def __init__(self, session_id: Optional[str], model_name: str, language: Optional[str]):
        ts = time.strftime("%Y%m%d-%H%M%S")
        self.session_id = session_id or f"session-{ts}"
        self.model = model_name
        self.language = language or "en"
        self.started_at = time.time()
        self.ended_at: Optional[float] = None
        self.jsonl_path = os.path.join(TRANSCRIPT_DIR, f"{self.session_id}.jsonl")
        self.summary_path = os.path.join(TRANSCRIPT_DIR, f"{self.session_id}.json")
        self._fh = open(self.jsonl_path, "a", encoding="utf-8")
        logging.info(f"[transcript] live → {self.jsonl_path}")

    def add_event(self, etype: str, t0: float, t1: float, text: str, uid: str, name: str):
        if etype != "final":
            return
        obj = {
            "time": time.time(),
            "type": etype,
            "t0": float(t0),
            "t1": float(t1),
            "text": text,
            "speaker": {"uid": uid, "name": name}
        }
        self._fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close_and_write_summary(self):
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()
        self.ended_at = time.time()
        summary = {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "model": self.model,
            "language": self.language,
            "jsonl": os.path.basename(self.jsonl_path)
        }
        tmp = self.summary_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.summary_path)
        logging.info(f"[transcript] summary → {self.summary_path}")
        return self.summary_path

# ──────────────────────────────────────────────────────────────────────────────
# Decode helpers (performance-logged; single-threaded via decode_lock)
# ──────────────────────────────────────────────────────────────────────────────
async def run_decode(
    chunks: List[np.ndarray],
    language: Optional[str],
    partial: bool = False,
) -> Tuple[str, float, float]:
    if not chunks:
        return "", 0.0, 0.0
    audio = np.concatenate(chunks, dtype=np.float32) if len(chunks) > 1 else chunks[0]

    t0 = time.time()
    segments, _ = model.transcribe(
        audio,
        language=language,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=200),
        beam_size=BEAM_PARTIAL if partial else BEAM_FINAL,
        condition_on_previous_text=COND_PREV,
        word_timestamps=False,
    )
    dt_ms = int((time.time() - t0) * 1000)
    dur_s  = len(audio) / float(TARGET_SR)
    rtf = (dt_ms / 1000.0) / max(dur_s, 1e-6)
    logging.info(f"[perf] {'partial' if partial else 'final'} decode_ms={dt_ms} dur_s={dur_s:.2f} RTF={rtf:.2f}")

    # Return last segment (most recent content)
    last_text, last_t0, last_t1 = "", 0.0, 0.0
    for seg in segments:
        last_text = (seg.text or "").strip()
        last_t0 = float(seg.start)
        last_t1 = float(seg.end)
    return last_text, last_t0, last_t1

async def decode_tail_partial(
    ws: web.WebSocketResponse,
    utter_buf: "Deque[np.ndarray]",
    language: Optional[str],
) -> None:
    """Decode only the last PARTIAL_WINDOW_SEC of utter_buf; debounce & dedupe."""
    try:
        if not utter_buf:
            return
        total_samples = sum(ch.size for ch in utter_buf)
        if total_samples <= 0:
            return

        last_total = getattr(ws, "_last_partial_total_samples", 0)  # type: ignore[attr-defined]
        if (total_samples - last_total) < int(MIN_PARTIAL_DELTA_SEC * TARGET_SR):
            return
        if getattr(ws, "_silence_hold", False):  # type: ignore[attr-defined]
            return

        need = int(PARTIAL_WINDOW_SEC * TARGET_SR)
        sel: List[np.ndarray] = []
        acc = 0
        for ch in reversed(utter_buf):
            if acc >= need:
                break
            sel.append(ch)
            acc += ch.size
        sel.reverse()

        text, t0, t1 = await run_decode(sel, language, partial=True)
        text = (text or "").strip()
        if not text:
            return

        sig = (text, round(t0, 2), round(t1, 2))
        last_sig = getattr(ws, "_last_partial_sig", ("", 0.0, 0.0))  # type: ignore[attr-defined]
        if sig == last_sig:
            return

        ws._last_partial_sig = sig                                # type: ignore[attr-defined]
        ws._last_partial_total_samples = total_samples            # type: ignore[attr-defined]
        ws._last_emit_ts = time.time()                            # type: ignore[attr-defined]

        uid, name = current_speaker_label()
        await ws.send_json({
            "type": "partial",
            "t0": t0, "t1": t1,
            "text": text,
            "speaker": {"uid": uid, "name": name},
        })
        logging.info(f"[partial][{name}] {text}")
    except Exception as e:
        logging.warning(f"Partial decode failed: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# WebSocket handler
# ──────────────────────────────────────────────────────────────────────────────
async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    if AUTH_TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {AUTH_TOKEN}":
            raise web.HTTPUnauthorized(text="invalid token")

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    logging.info("WS connected")

    started = False
    client_sr: Optional[int] = None
    language: Optional[str] = None
    session_id: Optional[str] = None

    pending: Deque[np.ndarray] = deque()    # rolling audio (global cap)
    utter_buf: Deque[np.ndarray] = deque()  # audio since last final

    last_decode_at = 0.0
    silence_ms = 0.0

    decode_lock = asyncio.Lock()
    ws._last_partial_sig = ("", 0.0, 0.0)     # type: ignore[attr-defined]
    ws._last_partial_total_samples = 0        # type: ignore[attr-defined]
    ws._silence_hold = False                  # type: ignore[attr-defined]
    ws._recorder: Optional[TranscriptRecorderJSONL] = None  # type: ignore[attr-defined]
    ws._last_emit_ts = time.time()            # type: ignore[attr-defined]
    ws._last_final_text = ""                  # type: ignore[attr-defined]
    ws._repeat_count = 0                      # type: ignore[attr-defined]

    # Watchdog: if no partial/final emitted in NO_PROGRESS_SEC, clear tails
    async def watchdog():
        while True:
            await asyncio.sleep(2.0)
            if time.time() - ws._last_emit_ts > NO_PROGRESS_SEC:  # type: ignore[attr-defined]
                logging.warning(f"[watchdog] no ASR output for >{NO_PROGRESS_SEC}s; clearing tail")
                utter_buf.clear()
                ws._last_partial_sig = ("", 0.0, 0.0)             # type: ignore[attr-defined]
                ws._last_partial_total_samples = 0                # type: ignore[attr-defined]
                ws._last_emit_ts = time.time()                    # type: ignore[attr-defined]
    wd_task = asyncio.create_task(watchdog())

    # Optional GC ticker to curb RSS drift
    gc_task = None
    if GC_EVERY_SEC > 0:
        async def gc_ticker():
            while True:
                await asyncio.sleep(GC_EVERY_SEC)
                gc.collect()
        gc_task = asyncio.create_task(gc_ticker())

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # Control messages: start/stop
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "error": "invalid_json"})
                    continue

                mtype = payload.get("type")
                if mtype == "start":
                    if started:
                        await ws.send_json({"type": "error", "error": "already_started"})
                        continue
                    client_sr = int(payload.get("sample_rate", TARGET_SR))
                    language  = payload.get("language", "en")
                    session_id = payload.get("session_id")
                    ws._recorder = TranscriptRecorderJSONL(session_id, MODEL_NAME, language)  # type: ignore[attr-defined]
                    started = True
                    await ws.send_json({"type": "ack", "message": "started"})
                    logging.info(f"Stream started: sr={client_sr}, lang={language}, session={ws._recorder.session_id}")  # type: ignore[attr-defined]

                elif mtype == "stop":
                    # Finalize any residual utterance
                    async with decode_lock:
                        final_text, t0, t1 = await run_decode(list(utter_buf), language)
                    if final_text:
                        uid, name = current_speaker_label()
                        await ws.send_json({
                            "type": "final",
                            "t0": t0, "t1": t1,
                            "text": final_text,
                            "speaker": {"uid": uid, "name": name},
                        })
                        if ws._recorder:
                            ws._recorder.add_event("final", t0, t1, final_text, uid, name)
                        logging.info(f"[final][{name}] {final_text}")

                    # Persist artifacts and optionally enqueue summary
                    if ws._recorder:
                        summary_path = ws._recorder.close_and_write_summary()

                        s3_url = None
                        if S3_BUCKET:
                            try:
                                s3_url = _upload_transcript_jsonl_to_s3(
                                    ws._recorder.jsonl_path, ws._recorder.session_id  # type: ignore[attr-defined]
                                )
                                await ws.send_json({"type": "uploaded", "transcript_s3": s3_url})
                            except Exception:
                                logging.exception("[s3] upload failed during stop")
                        else:
                            jsonl_abs = os.path.abspath(ws._recorder.jsonl_path)  # type: ignore[attr-defined]
                            s3_url = f"local://{jsonl_abs}"
                            logging.info(f"[local] transcript at {s3_url}")

                        # Summarizer enqueue (prefer presigned URL)
                        try:
                            presigned = None
                            if S3_BUCKET and s3_url and s3_url.startswith("s3://"):
                                ttl = int(os.getenv("PRESIGNED_TTL", "3600"))
                                presigned = _presigned_get_for_session(ws._recorder.session_id, expires=ttl)  # type: ignore[attr-defined]
                                await ws.send_json({"type": "uploaded", "transcript_presigned": presigned})
                            transcript_for_job = presigned or (s3_url or "")
                            if not transcript_for_job:
                                transcript_for_job = f"local://{os.path.abspath(ws._recorder.jsonl_path)}"  # type: ignore[attr-defined]
                                logging.warning(f"[enqueue] falling back to local path: {transcript_for_job}")
                            job = await enqueue_summary_job(
                                session_id=ws._recorder.session_id,  # type: ignore[attr-defined]
                                transcript_jsonl_abs=transcript_for_job,
                                model_name=os.getenv("SUMMARY_MODEL") or None
                            )
                            logging.info(f"[summarizer] enqueued job: {job}")
                        except Exception:
                            logging.exception("[summarizer] enqueue failed")

                        # Optional cleanup of local files
                        if os.getenv("DELETE_LOCAL_AFTER_UPLOAD", "false").lower() == "true":
                            try:
                                os.remove(ws._recorder.jsonl_path)  # type: ignore[attr-defined]
                            except Exception:
                                logging.exception("[cleanup] failed to remove transcript jsonl")
                            try:
                                os.remove(summary_path)
                            except Exception:
                                logging.exception("[cleanup] failed to remove summary json")

                    await ws.close()
                    logging.info("WS closed (stop)")
                    break

                else:
                    await ws.send_json({"type": "error", "error": "unknown_control_message"})

            elif msg.type == WSMsgType.BINARY:
                # Audio frames
                if not started:
                    await ws.send_json({"type": "error", "error": "send_start_first"})
                    continue

                audio = pcm16le_bytes_to_float32(msg.data)
                if client_sr and client_sr != TARGET_SR:
                    audio = simple_resample_linear(audio, client_sr, TARGET_SR)

                if audio.size:
                    pending.append(audio)
                    utter_buf.append(audio)

                    # Cap global rolling pending to MAX_SEC
                    def total_sec(dq: Deque[np.ndarray]) -> float:
                        return seconds_of_audio(sum(ch.size for ch in dq), TARGET_SR)

                    while total_sec(pending) > MAX_SEC:
                        pending.popleft()

                    # Backpressure trim if we are behind realtime
                    pend = total_sec(pending)
                    if pend > MAX_SEC * BACKPRESSURE_X:
                        logging.warning(f"[backpressure] pending={pend:.1f}s > {MAX_SEC*BACKPRESSURE_X:.1f}s; dropping oldest")
                        drop_s = pend - MAX_SEC
                        dropped = 0.0
                        while pending and dropped < drop_s:
                            ch = pending.popleft()
                            dropped += seconds_of_audio(ch.size, TARGET_SR)

                    # Silence tracking
                    rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
                    if rms < RMS_SILENT:
                        silence_ms += 1000.0 * seconds_of_audio(audio.size, TARGET_SR)
                        ws._silence_hold = True        # suppress partials in silence
                    else:
                        silence_ms = 0.0
                        ws._silence_hold = False

                # Periodic partials (tail only), never overlapping with finals
                total_samples_utter = sum(ch.size for ch in utter_buf)
                now_sec = seconds_of_audio(total_samples_utter, TARGET_SR)
                if now_sec - last_decode_at >= PARTIAL_SEC:
                    last_decode_at = now_sec
                    async def safe_partial():
                        async with decode_lock:
                            await decode_tail_partial(ws, utter_buf, language)
                    asyncio.create_task(safe_partial())

                # Final commit on silence or time-box breach
                if (silence_ms >= COMMIT_SIL_MS and total_samples_utter > 0) or \
                   (seconds_of_audio(total_samples_utter, TARGET_SR) >= MAX_UTTER_SEC):
                    async def commit_final():
                        async with decode_lock:
                            final_text, t0, t1 = await run_decode(list(utter_buf), language)
                            if final_text:
                                uid, name = current_speaker_label()
                                await ws.send_json({
                                    "type": "final",
                                    "t0": t0, "t1": t1,
                                    "text": final_text,
                                    "speaker": {"uid": uid, "name": name},
                                })
                                ws._last_emit_ts = time.time()    # type: ignore[attr-defined]

                                # Repeat-guard
                                if final_text == ws._last_final_text:  # type: ignore[attr-defined]
                                    ws._repeat_count += 1              # type: ignore[attr-defined]
                                else:
                                    ws._repeat_count = 0               # type: ignore[attr-defined]
                                ws._last_final_text = final_text       # type: ignore[attr-defined]

                                if ws._repeat_count >= REPEAT_GUARD_N: # type: ignore[attr-defined]
                                    logging.warning(f"[guard] repeated final x{ws._repeat_count+1}: '{final_text}' → clearing tails")
                                    utter_buf.clear()
                                    pending.clear()
                                    ws._last_partial_sig = ("", 0.0, 0.0)     # type: ignore[attr-defined]
                                    ws._last_partial_total_samples = 0        # type: ignore[attr-defined]
                                    ws._repeat_count = 0                       # type: ignore[attr-defined]

                                if ws._recorder:
                                    ws._recorder.add_event("final", t0, t1, final_text, uid, name)
                                logging.info(f"[final][{name}] {final_text}")

                            # Reset utterance with small tail for continuity
                            tail_keep = int(TAIL_KEEP_SEC * TARGET_SR)
                            keep_sel: Deque[np.ndarray] = deque()
                            kept = 0
                            for ch in reversed(utter_buf):
                                if kept >= tail_keep:
                                    break
                                keep_sel.appendleft(ch)
                                kept += ch.size
                            utter_buf.clear()
                            utter_buf.extend(keep_sel)

                            # Reset partial dedupe after a final
                            ws._last_partial_sig = ("", 0.0, 0.0)     # type: ignore[attr-defined]
                            ws._last_partial_total_samples = 0        # type: ignore[attr-defined]
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
        # Stop background tasks
        wd_task.cancel()
        if gc_task:
            gc_task.cancel()

        # Always finalize transcript even on abrupt disconnect
        try:
            if getattr(ws, "_recorder", None):
                ws._recorder.close_and_write_summary()  # type: ignore[attr-defined]
        except Exception:
            logging.exception("finalize-on-exit failed")

        await ws.close()
        logging.info("WS disconnected")
    return ws

# ──────────────────────────────────────────────────────────────────────────────
# Health & App
# ──────────────────────────────────────────────────────────────────────────────
async def healthz(_request: web.Request) -> web.Response:
    return web.Response(text="ok" if model_ready else "loading", status=200 if model_ready else 503)

def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/healthz", healthz)
    app.on_startup.append(_start_udp)
    app.on_cleanup.append(_stop_udp)
    return app

if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=PORT)
