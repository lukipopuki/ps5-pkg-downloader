# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1: build a self-contained virtualenv.
# Wheels are built here so the runtime image needs no compiler at all.
# ---------------------------------------------------------------------------
FROM python:3.13-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install --no-install-recommends -y build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && find /opt/venv -name "__pycache__" -type d -prune -exec rm -rf {} + \
 && find /opt/venv -name "*.pyc" -delete

# ---------------------------------------------------------------------------
# Stage 2: runtime.
# ---------------------------------------------------------------------------
FROM python:3.13-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="ps5-patch-downloader" \
      org.opencontainers.image.description="WebUI for downloading PS5 game updates from the official Sony CDN" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_DIR=/config \
    DOWNLOAD_DIR=/downloads \
    HOST=0.0.0.0 \
    PORT=8080

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/
COPY LICENSE README.md /app/

# A non-root default. On Unraid, run the container as 99:100 (nobody:users)
# so downloaded files match the rest of the array - see the README.
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --home-dir /app --no-create-home app \
 && mkdir -p /config /downloads \
 && chown -R app:app /app /config /downloads

USER app
EXPOSE 8080
VOLUME ["/config", "/downloads"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request,sys;\
u='http://127.0.0.1:%s/api/health'%os.environ.get('PORT','8080');\
sys.exit(0 if urllib.request.urlopen(u,timeout=4).status==200 else 1)"

# uvicorn's signal handling plus our lifespan hook turn SIGTERM/SIGINT into a
# clean shutdown: running transfers are paused and their progress persisted.
STOPSIGNAL SIGTERM
WORKDIR /app/backend
CMD ["python", "-m", "app.main"]
