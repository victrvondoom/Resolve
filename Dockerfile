# Untested template: see docs/DEPLOYMENT.md. Production mode; cloud browser stays disabled unless isolation is declared.
FROM python:3.12-slim
RUN useradd -m -u 10001 resolve
WORKDIR /app
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir pypdf reportlab pyyaml
COPY src ./src
COPY web ./web
COPY demo_data ./demo_data
COPY capabilities ./capabilities
COPY data ./data
RUN mkdir -p /data && chown -R resolve /data /app/data
USER resolve
ENV RESOLVE_MODE=production RESOLVE_BIND=0.0.0.0 RESOLVE_PORT=8765 \
    RESOLVE_DOCUMENTS_DIR=/data/documents RESOLVE_TENANTS_DB=/data/tenants.sqlite3 RESOLVE_DB=/data/resolve.sqlite3 \
    RESOLVE_SANDBOXES_DIR=/data/sandboxes RESOLVE_SANDBOXES_DB=/data/sandboxes.sqlite3 RESOLVE_CONNECTORS_DB=/data/connectors.sqlite3
# RESOLVE_ALLOWED_HOSTS must be supplied at deploy time or the server refuses to start.
EXPOSE 8765
HEALTHCHECK CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=3)" || exit 1
CMD ["python", "src/resolve_app.py"]
