# Atlas API image (also used for the worker). Build context = repo root:
#   docker build -f infra/api.Dockerfile -t atlas-api .
FROM python:3.13-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY backend/api/pyproject.toml backend/api/pyproject.toml
COPY backend/api/atlas_api backend/api/atlas_api
RUN python -m venv /opt/venv && /opt/venv/bin/pip install ./backend/api

FROM python:3.13-slim
RUN useradd --system --uid 10001 --no-create-home atlas
COPY --from=build /opt/venv /opt/venv
COPY contract/fixtures /app/contract/fixtures
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FIXTURES_DIR=/app/contract/fixtures \
    PORT=8000 \
    WEB_CONCURRENCY=2 \
    SHUTDOWN_GRACE_SECONDS=5
WORKDIR /app
USER atlas
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz',timeout=2)"
# --no-proxy-headers: request.client stays the real socket peer (the L7). The app takes the
# client IP from the rightmost X-Forwarded-For entry (TRUSTED_PROXY_HOPS), which the L7
# overwrites with the PROXY-protocol source, so clients can't pick their rate-limit bucket.
# On SIGTERM: /readyz -> 503 for SHUTDOWN_GRACE_SECONDS (LB drains), then graceful stop.
CMD ["sh", "-c", "exec uvicorn atlas_api.main:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY} --no-proxy-headers --timeout-graceful-shutdown 20 --no-server-header"]
