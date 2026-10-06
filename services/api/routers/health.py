from __future__ import annotations

import time

from fastapi import APIRouter
from sqlalchemy import text

from core.database import engine
from core.redis_client import get_redis

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness + dependency health check")
async def health() -> dict:
    """
    Returns healthy/degraded based on Postgres + Redis reachability.
    Prometheus scrapes this endpoint to track infra state.
    """
    checks: dict[str, str] = {}

    # Postgres
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {exc}"

    # Redis
    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"

    all_ok = all(v == "ok" for v in checks.values())
    return {
        "status": "healthy" if all_ok else "degraded",
        "checks": checks,
        "ts": time.time(),
    }
