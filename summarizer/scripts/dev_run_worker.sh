#!/usr/bin/env bash
set -euo pipefail
export $(grep -v '^#' .env 2>/dev/null | xargs -0 -I{} echo {} | tr '\n' ' ')
uvicorn app.api.main:app --reload --host 0.0.0.0 --port ${PORT:-9000}
