# syntax=docker/dockerfile:1
#
# BudgetSwarm backend image (BS-103). Multi-stage build:
#   base         shared Python runtime + installed dependencies (cached layer)
#   development  hot-reload server; source is bind-mounted by compose
#   production   lean, non-root, baked source, no reload, with a HEALTHCHECK
#
# Dev compose builds `--target development` and bind-mounts ./backend, ./ai_engine
# and ./data over /app (see docker-compose.yml), so edits reload live. The
# production target bakes the backend source and runs as an unprivileged user;
# ai_engine + its reference data are provided at deploy time the same way compose
# mounts them (the real engine stays gated behind ENABLE_REAL_ENGINE).

FROM python:3.11-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
# Install dependencies first so this layer is cached across source-only changes.
# All runtime deps ship wheels (psycopg[binary], ortools), so no build toolchain
# or system libs are required.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# --- development ------------------------------------------------------------
# Source is bind-mounted by compose, so nothing is copied here; --reload picks
# up host edits. This target reproduces the original dev image behaviour.
FROM base AS development
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# --- production -------------------------------------------------------------
FROM base AS production
# Bake the application source into the image (dev overlays this with a bind mount).
COPY . .
# Run as an unprivileged user — never as root (hardening).
RUN useradd --system --create-home --uid 10001 appuser \
 && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
# Liveness/readiness: the API answers 200 at /api/health even when the DB is
# unreachable (it reports {"db": "down"}); a refused connection or non-200 fails
# the check. No extra tooling needed — urllib ships with Python.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
