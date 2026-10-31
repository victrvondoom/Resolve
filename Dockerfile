# Production image; built and smoke-tested by scripts/docker_smoke.py (see docs/DEPLOYMENT.md).
# Cloud browser stays disabled unless isolation is declared.
# BASE_IMAGE can point at a registry mirror (e.g. mirror.gcr.io/library/python:3.12-slim) when Docker Hub rate-limits.
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
# WITH_STT=1 installs vosk and the offline English model (~40 MB) so /api/voice/transcribe-audio works with no API key.
ARG WITH_STT=0
RUN useradd -m -u 10001 resolve
WORKDIR /app
COPY requirements-dev.txt ./
# Optional build secret `ca` (an extra CA bundle) for builds behind a TLS-inspecting proxy; never stored in the image.
RUN --mount=type=secret,id=ca,required=false sh -c 'if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    pip install --no-cache-dir pypdf reportlab pyyaml cryptography' 
COPY src ./src
COPY web ./web
COPY apps ./apps
COPY demo_data ./demo_data
COPY capabilities ./capabilities
COPY data ./data
COPY scripts/fetch_stt_model.py ./scripts/fetch_stt_model.py
RUN --mount=type=secret,id=ca,required=false sh -c 'if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca SSL_CERT_FILE=/run/secrets/ca; fi; \
    if [ "$WITH_STT" = "1" ]; then pip install --no-cache-dir vosk && python scripts/fetch_stt_model.py; fi' 
RUN mkdir -p /data && chown -R resolve /data /app/data
USER resolve
ENV RESOLVE_MODE=production RESOLVE_BIND=0.0.0.0 RESOLVE_PORT=8765 \
    RESOLVE_DOCUMENTS_DIR=/data/documents RESOLVE_TENANTS_DB=/data/tenants.sqlite3 RESOLVE_DB=/data/resolve.sqlite3 \
    RESOLVE_SANDBOXES_DIR=/data/sandboxes RESOLVE_SANDBOXES_DB=/data/sandboxes.sqlite3 RESOLVE_CONNECTORS_DB=/data/connectors.sqlite3
# RESOLVE_ALLOWED_HOSTS and RESOLVE_MASTER_KEY (urlsafe base64, 32 bytes) must be supplied at deploy time or the server refuses to start.
EXPOSE 8765
HEALTHCHECK CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=3)" || exit 1
CMD ["python", "src/resolve_app.py"]
