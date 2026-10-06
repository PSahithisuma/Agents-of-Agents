"""
core/metrics.py — Prometheus metrics for the VibeForge API

Provides:
  - /metrics endpoint (via prometheus-fastapi-instrumentator for HTTP metrics)
  - Custom counters/histograms for business-level events

Usage in main.py:
    from core.metrics import setup_metrics
    setup_metrics(app)
"""
from __future__ import annotations

from prometheus_client import Counter, Histogram
from prometheus_fastapi_instrumentator import Instrumentator

# =============================================================================
# Business-level metrics (labelled for Grafana breakdown)
# =============================================================================

JOBS_ENQUEUED = Counter(
    "vibeforge_jobs_enqueued_total",
    "Total number of generation jobs enqueued",
    ["job_type"],
)

JOBS_CACHE_HITS = Counter(
    "vibeforge_spec_cache_hits_total",
    "Number of times a canonical_hash match prevented redundant generation (P3)",
    ["tenant_id"],
)

SPEC_COMPILED = Counter(
    "vibeforge_specs_compiled_total",
    "Total number of AppSpec objects compiled by the option-graph compiler",
    ["ui_framework", "db_tier", "auth_strategy"],
)

SPEC_COMPILE_DURATION = Histogram(
    "vibeforge_spec_compile_duration_seconds",
    "Time taken to compile an AppSpec from option selections",
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.5],
)

PROJECTS_CREATED = Counter(
    "vibeforge_projects_created_total",
    "Total projects created",
)


# =============================================================================
# Setup — call once during app lifespan
# =============================================================================

def setup_metrics(app) -> None:
    """
    Attach prometheus-fastapi-instrumentator to the FastAPI app.
    Exposes standard HTTP metrics at /metrics:
      - http_requests_total{method, handler, status}
      - http_request_duration_seconds{method, handler} (p50/p95/p99 histogram)
    """
    Instrumentator(
        should_group_status_codes=False,
        excluded_handlers=["/metrics", "/health"],
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
