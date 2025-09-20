FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code
COPY app ./app
COPY .env.example ./

# Runtime
ENV PORT=9000
EXPOSE 9000

# Healthcheck depends on python only (no curl)
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request,os; \
    u=f'http://127.0.0.1:{os.environ.get(\"PORT\",\"9000\")}/healthz'; \
    exit(0) if urllib.request.urlopen(u, timeout=2).status==200 else exit(1)" || exit 1

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "9000"]
