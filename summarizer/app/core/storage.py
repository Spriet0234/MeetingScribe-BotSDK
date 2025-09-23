# app/core/storage.py
import json
from pathlib import Path
from typing import Tuple
import os

import boto3
import requests

from app.core.config import settings


def _local_path(url: str) -> Path:
    assert url.startswith("local://")
    return Path(url.replace("local://", "", 1)).resolve()


def _read_http(url: str) -> str:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.text


def _read_s3(url: str) -> str:
    # url like: s3://bucket/key...
    _, _, rest = url.partition("s3://")
    bucket, _, key = rest.partition("/")
    s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1"))
    obj = s3.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read().decode("utf-8")


def _jsonl_to_plain_text(raw: str) -> str:
    """Turn finals from JSONL lines into '[mm:ss] Speaker: text' lines."""
    lines = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if (e.get("type") or "").lower() != "final":
            continue
        t0 = float(e.get("t0") or 0.0); mm = int(t0 // 60); ss = int(t0 % 60)
        sp = (e.get("speaker") or {}).get("name") or "Unknown"
        txt = (e.get("text") or "").strip()
        if txt:
            lines.append(f"[{mm:02d}:{ss:02d}] {sp}: {txt}")
    return "\n".join(lines)


def _json_to_plain_text(raw: str) -> str:
    try:
        obj = json.loads(raw)
    except Exception:
        return raw
    finals = []
    for e in obj.get("events", []):
        if (e.get("type") or "").lower() == "final":
            finals.append(e)
    # Reuse the same formatting
    return _jsonl_to_plain_text("\n".join(json.dumps(e, ensure_ascii=False) for e in finals))


def read_transcript(url: str) -> str:
    """
    Returns plain text suitable for summarization.
    Supports:
      - local://abs/path.jsonl
      - s3://bucket/key.jsonl
      - http(s)://... (including presigned GET URLs)
      - direct filesystem paths
    """
    # 1) fetch raw
    if url.startswith("local://"):
        p = _local_path(url)
        raw = p.read_text(encoding="utf-8")
        suffix = p.suffix
    elif url.startswith("s3://"):
        raw = _read_s3(url)
        suffix = ".jsonl"  # our ASR uploads jsonl
    elif url.startswith("http://") or url.startswith("https://"):
        raw = _read_http(url)
        suffix = ".jsonl"  # presigned GET of jsonl
    else:
        p = Path(url)
        raw = p.read_text(encoding="utf-8")
        suffix = p.suffix

    # 2) normalize json/jsonl to plain text
    if suffix == ".jsonl":
        try:
            return _jsonl_to_plain_text(raw)
        except Exception:
            return raw
    if suffix == ".json":
        try:
            return _json_to_plain_text(raw)
        except Exception:
            return raw
    return raw


def write_results(session_id: str, obj: dict, md: str) -> Tuple[str, str]:
    # Write to local path defined by RESULTS_BUCKET (e.g., local://data/summaries)
    base = _local_path(f"{settings.RESULTS_BUCKET.rstrip('/')}/{session_id}").with_suffix(".summary")
    base.parent.mkdir(parents=True, exist_ok=True)
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    json_path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(md, encoding="utf-8")
    return (f"local://{json_path}", f"local://{md_path}")
