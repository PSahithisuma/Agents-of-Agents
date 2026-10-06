# =============================================================================
# Python/FastAPI + React Toolchain Image (Phase 3 — Contract C12)
# Used by Cloud Build QA Gate for Python FastAPI + React jobs
#
# Contains: Python 3.12, Node 20, uv, pytest, ruff, mypy, alembic,
#           Semgrep, Trivy, gitleaks
# =============================================================================

FROM python:3.12-slim AS base

LABEL org.opencontainers.image.title="VibeForge Python+React Toolchain"
LABEL org.opencontainers.image.version="3.12"

# ── System tools ──────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl git wget unzip ca-certificates gnupg \
    && rm -rf /var/lib/apt/lists/*

# ── Node 20 (for React builds) ────────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── uv (fast pip replacement) ─────────────────────────────────────────────────
RUN pip install uv

# ── Python dev tools ──────────────────────────────────────────────────────────
RUN uv pip install --system --no-cache \
    pytest \
    pytest-cov \
    pytest-asyncio \
    httpx \
    ruff \
    mypy \
    alembic \
    uvicorn \
    fastapi \
    sqlalchemy \
    psycopg2-binary \
    semgrep

# ── Trivy ─────────────────────────────────────────────────────────────────────
RUN wget -q "https://github.com/aquasecurity/trivy/releases/download/v0.50.0/trivy_0.50.0_Linux-64bit.tar.gz" \
    -O /tmp/trivy.tar.gz \
    && tar -xzf /tmp/trivy.tar.gz -C /usr/local/bin trivy \
    && rm /tmp/trivy.tar.gz

# ── gitleaks ──────────────────────────────────────────────────────────────────
RUN wget -q "https://github.com/gitleaks/gitleaks/releases/download/v8.18.0/gitleaks_8.18.0_linux_x64.tar.gz" \
    -O /tmp/gitleaks.tar.gz \
    && tar -xzf /tmp/gitleaks.tar.gz -C /usr/local/bin gitleaks \
    && rm /tmp/gitleaks.tar.gz

# ── pip config — points to Artifact Registry PyPI proxy ──────────────────────
# Baked in; Cloud Build overrides via --index-url flag
RUN mkdir -p /root/.config/pip
COPY pip.conf /root/.config/pip/pip.conf

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /workspace
