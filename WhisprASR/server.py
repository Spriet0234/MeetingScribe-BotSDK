#!/usr/bin/env python3
import os
import json
import time
import asyncio
import logging
from typing import Optional, Deque, Tuple, Dict, Any, List
from collections import deque

import numpy as np
from aiohttp import web, WSMsgType
from faster_whisper import WhisperModel

# ───────── Config ─────────
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

# Silence/commit tuning
RMS_SILENT    = float(os.getenv("RMS_SILENT", "0.002"))
COMMIT_SIL_MS = float(os.getenv("COMMIT_SIL_MS", "800.0"))
TAIL_KEEP_SEC = float(os.getenv("TAIL_KEEP_SEC", "0.5"))
MAX_SEC       = float(os.getenv("MAX_SEC", "20.0"))    # rolling buffer cap for pending
MAX_UTTER_SEC = float(os.getenv("MAX_UTTER_SEC", "12.0"))  # force final if utterance too long

# Live JSONL writing toggle (always on here)
LIVE_JSONL = True

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

# ───────── Model ─────────
logging.info(f"Loading model: {MODEL_NAME} on {DEVICE} ({COMPUTE}) …")
model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE)
model_ready = True
logging.info("Model ready.")

# ───────── Speaker state (fed by UDP from the bot) ─────────
current_active_ids: List[str] = []
name_map: Dict[str, str] = {}

def current_speaker_label() -> Tuple[str, str]:
    uid = current_active_ids[0] if current_active_ids else ""
    return (uid, name_map.get(uid, f"User {uid}") if uid else "Unknown")

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

async def _start_udp(app):
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

async def _stop_udp(app):
    for t in app.get("udp_transports", []):
        t.close()

# ───────── IO helpers ─────────
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

# ───────── Recorders ─────────
class TranscriptRecorderJSONL:
    """
    Live JSONL recorder:
      - appends one JSON object per line as events arrive
      - writes to /data/transcripts/<session_id>.jsonl
      - also collects minimal metadata for an end-of-session summary JSON
    """
    def __init__(self, session_id: Optional[str], model_name: str, language: Optional[str]):
        ts = time.strftime("%Y%m%d-%H%M%S")
        self.session_id = session_id or f"session-{ts}"
        self.model = model_name
        self.language = language or "en"
        self.started_at = time.time()
        self.ended_at: Optional[float] = None
        self.jsonl_path = os.path.join(TRANSCRIPT_DIR, f"{self.session_id}.jsonl")
        self.summary_path = os.path.join(TRANSCRIPT_DIR, f"{self.session_id}.json")
        # open for append in text mode, utf-8
        self._fh = open(self.jsonl_path, "a", encoding="utf-8")
        logging.info(f"[transcript] live → {self.jsonl_path}")

    def add_event(self, etype: str, t0: float, t1: float, text: str, uid: str, name: str):
    # 🔒 Only persist finals; skip partials entirely
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

# ───────── Decode helpers ─────────
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
        beam_size=1 if partial else 5,
        condition_on_previous_text=True,
        word_timestamps=False,
    )
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
    """Decode only the last N seconds of the current utterance; debounce and dedupe."""
    try:
        PARTIAL_WINDOW_SEC = 6.0
        MIN_PARTIAL_DELTA_SEC = 0.4

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

        text, t0, t1 = await run_decode(sel, 0, language, partial=True)
        text = (text or "").strip()
        if not text:
            return

        sig = (text, round(t0, 2), round(t1, 2))
        last_sig = getattr(ws, "_last_partial_sig", ("", 0.0, 0.0))  # type: ignore[attr-defined]
        if sig == last_sig:
            return
        ws._last_partial_sig = sig                               # type: ignore[attr-defined]
        ws._last_partial_total_samples = total_samples           # type: ignore[attr-defined]

        uid, name = current_speaker_label()
        await ws.send_json({
            "type": "partial",
            "t0": t0, "t1": t1,
            "text": text,
            "speaker": {"uid": uid, "name": name},
        })
        if ws._recorder:
            ws._recorder.add_event("partial", t0, t1, text, uid, name)  # type: ignore[attr-defined]
        logging.info(f"[partial][{name}] {text}")
    except Exception as e:
        logging.warning(f"Partial decode failed: {e}")

# ───────── WS Handler ─────────
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
    session_id: Optional[str] = None

    pending: Deque[np.ndarray] = deque()   # rolling audio (global cap)
    utter_buf: Deque[np.ndarray] = deque() # audio since last final

    last_decode_at = 0.0
    silence_ms = 0.0

    decode_lock = asyncio.Lock()
    ws._last_partial_sig = ("", 0.0, 0.0)      # type: ignore[attr-defined]
    ws._last_partial_total_samples = 0         # type: ignore[attr-defined]
    ws._silence_hold = False                   # type: ignore[attr-defined]
    ws._recorder: Optional[TranscriptRecorderJSONL] = None  # type: ignore[attr-defined]

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
                    language  = payload.get("language", "en")
                    session_id = payload.get("session_id")
                    ws._recorder = TranscriptRecorderJSONL(session_id, MODEL_NAME, language)  # type: ignore[attr-defined]
                    started = True
                    await ws.send_json({"type": "ack", "message": "started"})
                    logging.info(f"Stream started: sr={client_sr}, lang={language}, session={ws._recorder.session_id}")  # type: ignore[attr-defined]

                elif payload.get("type") == "stop":
                    async with decode_lock:
                        final_text, t0, t1 = await run_decode(list(utter_buf), 0, language)
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

                    if ws._recorder:
                        ws._recorder.close_and_write_summary()
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
                    utter_buf.append(audio)

                    # Cap rolling pending
                    while seconds_of_audio(sum(ch.size for ch in pending), TARGET_SR) > MAX_SEC:
                        pending.popleft()

                    # Silence tracking on the new chunk
                    rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
                    if rms < RMS_SILENT:
                        silence_ms += 1000.0 * seconds_of_audio(audio.size, TARGET_SR)
                        ws._silence_hold = True       # suppress partials during silence
                    else:
                        silence_ms = 0.0
                        ws._silence_hold = False

                # Periodic partial (tail of current utterance)
                total_samples_utter = sum(ch.size for ch in utter_buf)
                now_sec = seconds_of_audio(total_samples_utter, TARGET_SR)
                if now_sec - last_decode_at >= PARTIAL_SEC:
                    last_decode_at = now_sec
                    async def safe_partial():
                        async with decode_lock:
                            await decode_tail_partial(ws, utter_buf, language)
                    asyncio.create_task(safe_partial())

                # Commit conditions: silence or utterance too long
                if (silence_ms >= COMMIT_SIL_MS and total_samples_utter > 0) or \
                   (seconds_of_audio(total_samples_utter, TARGET_SR) >= MAX_UTTER_SEC):
                    async def commit_final():
                        async with decode_lock:
                            final_text, t0, t1 = await run_decode(list(utter_buf), 0, language, partial=False)
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

                            # Reset utterance buffer with a small tail
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

                            ws._last_partial_sig = ("", 0.0, 0.0)       # reset dedupe
                            ws._last_partial_total_samples = 0
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
        # Always close and write summary, even if client dropped
        try:
            if ws._recorder:
                ws._recorder.close_and_write_summary()
        except Exception:
            logging.exception("finalize-on-exit failed")
        await ws.close()
        logging.info("WS disconnected")
    return ws

# ───────── Health & App ─────────
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
