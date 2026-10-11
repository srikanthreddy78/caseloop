# Build from the repository root so the image also carries the widget and the dashboard:
#   docker build -f backend/Dockerfile -t caseloop .
# The API serves /widget.js and /dashboard from ../widget and ../dashboard/dist relative to
# backend/app, so the runtime layout mirrors the repo: /srv/backend, /srv/widget, /srv/dashboard.

# ---- dashboard: static bundle -------------------------------------------------------------
FROM node:20-alpine AS dashboard
WORKDIR /build
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY dashboard/ ./
RUN npm run build

# ---- backend: virtualenv with locked dependencies ------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS backend
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /srv/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime: slim image, non-root, migrations then the server -----------------------------
FROM python:3.13-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/srv/backend/.venv/bin:$PATH" \
    CASELOOP_ENV=prod \
    CASELOOP_DATA_DIR=/data \
    PORT=8000

RUN groupadd --system caseloop && useradd --system --gid caseloop --home /srv caseloop \
    && mkdir -p /data && chown caseloop:caseloop /data

WORKDIR /srv/backend
COPY --from=backend --chown=caseloop:caseloop /srv/backend /srv/backend
COPY --chown=caseloop:caseloop widget /srv/widget
COPY --from=dashboard --chown=caseloop:caseloop /build/dist /srv/dashboard/dist

USER caseloop
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:' + __import__('os').environ.get('PORT', '8000') + '/healthz', timeout=4).status == 200 else 1)"

# Apply migrations, then hand the process over to uvicorn (exec keeps it PID 1 for signals).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
