import json
from pathlib import Path
from typing import Tuple
from app.core.config import settings

def _local_path(url: str) -> Path:
    assert url.startswith("local://")
    return Path(url.replace("local://","",1)).resolve()

def read_transcript(url: str) -> str:
    # For MVP: support local finals-only .jsonl or .json; extend to S3 as needed
    p = _local_path(url) if url.startswith("local://") else Path(url)
    text = p.read_text(encoding="utf-8")
    # If JSONL/JSON -> flatten to plain text lines "[mm:ss] Speaker: text"
    if p.suffix in (".jsonl",".json"):
        from app.worker.worker import read_events, to_plain_text  # reuse helper
        evts = read_events(str(p))
        return to_plain_text(evts)
    return text

def write_results(session_id: str, obj: dict, md: str) -> Tuple[str, str]:
    base = _local_path(f"{settings.RESULTS_BUCKET.rstrip('/')}/{session_id}").with_suffix(".summary")
    base.parent.mkdir(parents=True, exist_ok=True)
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    json_path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(md, encoding="utf-8")
    return (f"local://{json_path}", f"local://{md_path}")
