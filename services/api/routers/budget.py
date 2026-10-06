"""
Budget & Observability Router (Contract C11, Phase 3)
GET /api/v1/budget/{tenant_id}/summary  — per-tenant LLM spend from Redis
GET /api/v1/budget/{tenant_id}/history  — 7-day history
GET /api/v1/gate/stats                  — gate pass/fail rates from DB
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/v1/budget", tags=["budget"])

_REDIS_HOST     = os.environ.get("REDIS_HOST", "localhost")
_REDIS_PORT     = int(os.environ.get("REDIS_PORT", "6379"))
_REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")

# Single shared URL — always includes password if set
_auth      = f":{_REDIS_PASSWORD}@" if _REDIS_PASSWORD else ""
_REDIS_URL = f"redis://{_auth}{_REDIS_HOST}:{_REDIS_PORT}"


# Shared connection pool — created once, reused across all requests
# Prevents "Too many connections" from per-request pool creation
_pool: aioredis.Redis | None = None

def _redis() -> aioredis.Redis:
    global _pool
    if _pool is None:
        _pool = aioredis.from_url(
            _REDIS_URL,
            decode_responses=True,
            max_connections=10,   # cap per-service pool
        )
    return _pool


async def _jobs_per_day(tenant_id: str, days: int) -> dict:
    """Count jobs per day from the jobs table (the source of truth)."""
    from sqlalchemy import text
    from core.database import get_tenant_session
    from uuid import UUID
    out = {}
    async with get_tenant_session(UUID(tenant_id)) as session:
        rows = await session.execute(
            text("SELECT created_at::date AS d, count(*) AS n FROM jobs "
                 "WHERE created_at >= now() - make_interval(days => :d) GROUP BY 1"),
            {"d": days},
        )
        for d, n in rows:
            out[d.isoformat()] = int(n)
    return out


@router.get("/{tenant_id}/summary")
async def get_budget_summary(tenant_id: str):
    """Today's job count (from the jobs table) and LLM tokens (from Redis counters)."""
    today = date.today().isoformat()
    data = await _redis().hgetall(f"budget:{tenant_id}:{today}")
    jobs = (await _jobs_per_day(tenant_id, 1)).get(today, 0)
    tokens = int(data.get("llm_tokens", 0))
    return {
        "tenant_id": tenant_id,
        "date": today,
        "llm_tokens_today": tokens,
        "jobs_today": jobs,
        "estimated_cost_usd": round(tokens * 0.000002, 4),
    }


@router.get("/{tenant_id}/history")
async def get_budget_history(tenant_id: str, days: int = 7):
    """Per-day jobs (jobs table) and LLM tokens (Redis counters) for the last N days."""
    r = _redis()
    jobs = await _jobs_per_day(tenant_id, days)
    history = []
    for i in range(days):
        day = (date.today() - timedelta(days=i)).isoformat()
        data = await r.hgetall(f"budget:{tenant_id}:{day}")
        tokens = int(data.get("llm_tokens", 0))
        history.append({
            "date": day,
            "llm_tokens": tokens,
            "jobs": jobs.get(day, 0),
            "estimated_cost_usd": round(tokens * 0.000002, 4),
        })
    return {"tenant_id": tenant_id, "history": list(reversed(history))}


metrics_router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])


@metrics_router.get("/summary")
async def metrics_summary():
    """Dashboard KPIs from the database. Values the DB cannot know are returned as 0."""
    from sqlalchemy import text
    from core.database import get_tenant_session
    from uuid import UUID
    tenant = UUID("00000000-0000-0000-0000-000000000001")
    async with get_tenant_session(tenant) as session:
        j = (await session.execute(text(
            "SELECT count(*) FILTER (WHERE created_at > now() - interval '30 days'), "
            "count(*) FILTER (WHERE status = 'completed' AND created_at > now() - interval '30 days'), "
            "count(*) FILTER (WHERE status = 'failed' AND created_at > now() - interval '30 days') FROM jobs"))).one()
        spend = (await session.execute(text(
            "SELECT coalesce(sum(cost_usd), 0) FROM llm_calls WHERE created_at > now() - interval '30 days'"))).scalar_one()
    return {
        "jobs_30d": int(j[0]), "jobs_completed_30d": int(j[1]), "jobs_failed_30d": int(j[2]),
        "llm_spend_usd_30d": float(spend), "llm_spend_delta": 0.0,
    }
