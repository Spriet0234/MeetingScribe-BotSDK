import asyncio, json, uuid
from typing import Optional
import redis.asyncio as redis
from app.core.config import settings
from app.core.models import JobCreate, JobStatus

JOB_NS = "summarizer:jobs"
QUEUE_KEY = f"{JOB_NS}:queue"
STATUS_KEY = f"{JOB_NS}:status"

_r = redis.from_url(settings.QUEUE_URL, decode_responses=True)

async def enqueue_job(job: JobCreate) -> str:
    job_id = str(uuid.uuid4())
    await _r.hset(STATUS_KEY, job_id, json.dumps({"job_id": job_id, "status": "queued"}))
    await _r.rpush(QUEUE_KEY, json.dumps(job.model_dump() | {"job_id": job_id}))
    return job_id

async def get_status(job_id: str) -> Optional[JobStatus]:
    raw = await _r.hget(STATUS_KEY, job_id)
    return JobStatus(**json.loads(raw)) if raw else None

async def set_status(job_id: str, **fields):
    raw = await _r.hget(STATUS_KEY, job_id)
    cur = json.loads(raw) if raw else {"job_id": job_id}
    cur.update(fields)
    await _r.hset(STATUS_KEY, job_id, json.dumps(cur))
