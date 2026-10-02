# Moraa GemVision backend image (DEP-1). Build from the backend/ folder:   docker build -t moraa-backend backend
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# pg_dump / pg_restore for scripts/db_backup.py and scripts/db_restore_check.py; curl for the health check.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install -r requirements.txt -c constraints.txt

COPY . .
RUN useradd --create-home --uid 10001 moraa \
    && mkdir -p /data/uploads /data/reports /data/logs \
    && chown -R moraa:moraa /data /app
USER moraa

ENV ENVIRONMENT=production \
    HOST=0.0.0.0 \
    PORT=8000 \
    UPLOAD_DIR=/data/uploads \
    REPORT_DIR=/data/reports \
    LOG_DIR=/data/logs \
    WEB_CONCURRENCY=2

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health/live || exit 1

# --timeout-graceful-shutdown: on a deploy the server stops taking new work but lets orders already running (a Catalog
# Pack can take a few minutes) finish; anything still unfinished is refunded by the recovery sweep.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY} --timeout-graceful-shutdown 120"]
