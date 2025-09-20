from fastapi import APIRouter, BackgroundTasks, HTTPException
from app.core.models import JobCreate, JobStatus
from app.core.queue import enqueue_job, get_status, JOB_NS

router = APIRouter()

@router.post("", response_model=JobStatus)
async def create_job(job: JobCreate):
    job_id = await enqueue_job(job)
    return JobStatus(job_id=job_id, status="queued")

@router.get("/{job_id}", response_model=JobStatus)
async def get_job(job_id: str):
    st = await get_status(job_id)
    if not st:
        raise HTTPException(status_code=404, detail="job not found")
    return st
