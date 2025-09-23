# app/worker/worker.py
import json, asyncio

from app.core.queue import _r, QUEUE_KEY, set_status
from app.core.summarizer import summarize_text, to_markdown
from app.core.storage import read_transcript, write_results


async def run_once():
    raw = await _r.blpop(QUEUE_KEY, timeout=5)  # blocking pop
    if not raw:
        return
    _, payload = raw
    job = json.loads(payload)
    job_id = job["job_id"]
    await set_status(job_id, status="processing")

    try:
        text = job.get("transcript_text") or read_transcript(job["transcript_url"])
        result = await summarize_text(text, model=job.get("model"))
        md = to_markdown(result)
        session_id = job.get("session_id", "session")
        json_url, md_url = write_results(session_id, result, md)
        await set_status(job_id, status="done", result_url=str(json_url))
    except Exception as e:
        await set_status(job_id, status="failed", error=str(e))


async def main():
    while True:
        await run_once()


if __name__ == "__main__":
    asyncio.run(main())
