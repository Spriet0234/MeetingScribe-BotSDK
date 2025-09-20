FROM python:3.11-slim

# Add curl for healthcheck (and keep ffmpeg if you want it)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir aiohttp faster-whisper numpy

COPY server.py /app/server.py

ENV MODEL=small.en DEVICE=cpu COMPUTE=int8 PORT=8080 DATA_DIR=/data
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD curl -fsS http://localhost:${PORT}/healthz || exit 1

CMD ["python","/app/server.py"]
