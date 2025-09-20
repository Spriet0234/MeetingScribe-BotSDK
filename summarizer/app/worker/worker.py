import os, json, asyncio
from pathlib import Path
from typing import List, Dict, Any

from app.core.queue import _r, QUEUE_KEY, set_status
from app.core.summarizer import summarize_text, to_markdown
from app.core.storage import read_transcript, write_results

# Helpers copied from your earlier tool so storage can import them if needed:
def read_events(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    evts = []
    if p.suffix == ".jsonl":
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip(): continue
            obj = json.loads(line)
            if obj.get("type") == "final":
                evts.append(obj)
    else:
        obj = json.loads(p.read_text(encoding="utf-8"))
        for e in obj.get("events", []):
            if e.get("type") == "final":
                evts.append(e)
    return evts

def to_plain_text(evts: List[Dict[str, Any]]) -> str:
    lines = []
    for e in evts:
        t0 = float(e.get("t0") or 0.0); mm=int(t0//60); ss=int(t0%60)
        sp = (e.get("speaker") or {}).get("name") or "Unknown"
        txt = (e.get("text") or "").strip()
        if txt: lines.append(f"[{mm:02d}:{ss:02d}] {sp}: {txt}")
    return "\n".join(lines)

async def run_once():
    raw = await _r.blpop(QUEUE_KEY, timeout=5)  # blocking pop
    if not raw: 
        return
    _, payload = raw
    job = json.loads(payload)
    job_id = job["job_id"]
    await set_status(job_id, status="processing")

    try:
        if job.get("transcript_text"):
            text = job["transcript_text"]
        else:
            text = read_transcript(job["transcript_url"])
        result = await summarize_text(text, model=job.get("model"))
        md = to_markdown(result)
        session_id = job.get("session_id","session")
        json_url, md_url = write_results(session_id, result, md)
        await set_status(job_id, status="done", result_url=str(json_url))
    except Exception as e:
        await set_status(job_id, status="failed", error=str(e))

async def main():
    while True:
        await run_once()

if __name__ == "__main__":
    asyncio.run(main())
