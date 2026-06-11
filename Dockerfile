# Production-ready container for NCGA. Uses waitress (multi-threaded) — wsgiref dev
# server is not used in this image.
#
# Build:   docker build -t ncga .
# Run:     docker run -p 8000:8000 --env-file .env ncga
#
# Recommended in front: a TLS-terminating reverse proxy (Caddy / Nginx / Traefik).
# This image binds 0.0.0.0:8000 directly; never expose without TLS in production.

FROM python:3.12-slim AS base

# Don't run as root.
RUN groupadd -r ncga && useradd -r -g ncga -u 1000 -m -d /home/ncga ncga

WORKDIR /app

# System deps for cryptography wheels (most are pure-python now, but keep CA + tini)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    tini \
    && rm -rf /var/lib/apt/lists/*

# Python deps first (better Docker layer caching)
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir waitress

# App code
COPY app.py ./
COPY native_chinese_assistant ./native_chinese_assistant
COPY static ./static
# Corpus + lexicon JSONL. Without this layer the BM25 few-shot / word-hint
# features silently disable (loaders return None when data/ is missing).
COPY data ./data

# Writable user data dir for quality store
RUN mkdir -p /home/ncga/.local/share/ncga \
 && chown -R ncga:ncga /home/ncga /app

USER ncga
ENV PYTHONUNBUFFERED=1 \
    NCGA_HOST=0.0.0.0 \
    NCGA_PORT=8000

EXPOSE 8000

# tini is PID 1 to handle signals correctly (so SIGTERM from `docker stop` is graceful).
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["waitress-serve", "--host=0.0.0.0", "--port=8000", "--threads=8", \
     "native_chinese_assistant.web:application"]

# Healthcheck via the existing /api/healthz endpoint.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python3 -c "import urllib.request, sys; \
      sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/healthz', timeout=3).status == 200 else 1)"
