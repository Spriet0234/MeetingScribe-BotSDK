from pydantic import BaseModel, HttpUrl
from typing import Optional, List, Dict, Any

class JobCreate(BaseModel):
    session_id: str
    transcript_url: Optional[str] = None    
    transcript_text: Optional[str] = None    # alternative: raw text
    callback_url: Optional[str] = None       # webhook
    mode: str = "sync"                      
    model: Optional[str] = None

class JobStatus(BaseModel):
    job_id: str
    status: str
    result_url: Optional[str] = None
    error: Optional[str] = None
