FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server.py /app/server.py

RUN pip install --no-cache-dir aiohttp faster-whisper numpy

ENV MODEL=small.en DEVICE=cpu COMPUTE=int8 PORT=8080
EXPOSE 8080
CMD ["python", "/app/server.py"]
