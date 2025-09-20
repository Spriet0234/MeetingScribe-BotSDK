from fastapi import FastAPI
from app.api.routes import health, jobs
from app.core.logging import setup_logging

setup_logging()
app = FastAPI(title="Summarizer API", version="0.1.0")

app.include_router(health.router, tags=["health"])
app.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
