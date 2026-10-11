"""Dashboard data for the UI: live job counts, results per pack, recent jobs, failures, slot usage.

All job queries run inside the caller's tenant session, so tenant isolation applies.
The UI polls these endpoints every 5-10 seconds; per-job progress uses /api/v1/jobs/{id}/stream.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text

from core.auth import AuthUser, get_current_user
from core.database import get_tenant_session

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

CAPACITY_URL = os.getenv("CAPACITY_URL", "http://capacity:8080")


class PackResult(BaseModel):
    pack: str
    completed: int
    failed: int


class DayResult(BaseModel):
    day: date
    completed: int
    failed: int


class Summary(BaseModel):
    days: int
    running: int
    waiting: int
    completed: int
    failed: int
    success_rate_pct: Optional[float]
    avg_run_minutes: Optional[float]
    by_pack: list[PackResult]
    by_day: list[DayResult]


class RecentJob(BaseModel):
    job_id: UUID
    created_at: datetime
    pack: str
    app_name: Optional[str]
    status: str
    minutes: Optional[float]


class FailedJob(BaseModel):
    job_id: UUID
    created_at: datetime
    pack: str
    error: Optional[str]


class Capacity(BaseModel):
    available: bool
    llm_slots_used: Optional[int] = None
    llm_slots_total: Optional[int] = None
    build_slots_used: Optional[int] = None
    build_slots_total: Optional[int] = None
    queue_total: Optional[int] = None


@router.get("/summary", response_model=Summary)
async def summary(
    days: int = Query(7, ge=1, le=90),
    user: AuthUser = Depends(get_current_user),
) -> Summary:
    async with get_tenant_session(user.tenant_id) as session:
        head = (
            await session.execute(
                text("""
                    SELECT
                      count(*) FILTER (WHERE status = 'running') AS running,
                      count(*) FILTER (WHERE status IN ('pending','queued')) AS waiting,
                      count(*) FILTER (WHERE status = 'completed'
                          AND created_at >= now() - make_interval(days => :days)) AS completed,
                      count(*) FILTER (WHERE status = 'failed'
                          AND created_at >= now() - make_interval(days => :days)) AS failed,
                      (avg(extract(epoch FROM finished_at - started_at)) FILTER (
                          WHERE status = 'completed'
                            AND created_at >= now() - make_interval(days => :days)) / 60.0)::float8 AS avg_min
                    FROM jobs
                """),
                {"days": days},
            )
        ).mappings().one()
        packs = (
            await session.execute(
                text("""
                    SELECT COALESCE(s.spec_data->>'vertical', 'n/a') AS pack,
                           count(*) FILTER (WHERE j.status = 'completed') AS completed,
                           count(*) FILTER (WHERE j.status = 'failed') AS failed
                    FROM jobs j JOIN application_specs s ON s.id = j.spec_id
                    WHERE j.status IN ('completed','failed')
                      AND j.created_at >= now() - make_interval(days => :days)
                    GROUP BY 1 ORDER BY 2 DESC
                """),
                {"days": days},
            )
        ).mappings().all()
        daily = (
            await session.execute(
                text("""
                    SELECT date_trunc('day', created_at)::date AS day,
                           count(*) FILTER (WHERE status = 'completed') AS completed,
                           count(*) FILTER (WHERE status = 'failed') AS failed
                    FROM jobs
                    WHERE status IN ('completed','failed')
                      AND created_at >= now() - make_interval(days => :days)
                    GROUP BY 1 ORDER BY 1
                """),
                {"days": days},
            )
        ).mappings().all()

    completed, failed = int(head["completed"]), int(head["failed"])
    done = completed + failed
    avg_min = head["avg_min"]
    return Summary(
        days=days,
        running=int(head["running"]),
        waiting=int(head["waiting"]),
        completed=completed,
        failed=failed,
        success_rate_pct=round(100.0 * completed / done, 1) if done else None,
        avg_run_minutes=round(avg_min, 1) if avg_min is not None else None,
        by_pack=[PackResult(**dict(r)) for r in packs],
        by_day=[DayResult(**dict(r)) for r in daily],
    )


@router.get("/recent", response_model=list[RecentJob])
async def recent(
    limit: int = Query(15, ge=1, le=50),
    user: AuthUser = Depends(get_current_user),
) -> list[RecentJob]:
    async with get_tenant_session(user.tenant_id) as session:
        rows = (
            await session.execute(
                text("""
                    SELECT j.id AS job_id, j.created_at,
                           COALESCE(s.spec_data->>'vertical', 'n/a') AS pack,
                           s.spec_data->>'app_name' AS app_name, j.status,
                           (extract(epoch FROM j.finished_at - j.started_at) / 60.0)::float8 AS minutes
                    FROM jobs j JOIN application_specs s ON s.id = j.spec_id
                    WHERE j.status <> 'cancelled'
                    ORDER BY j.created_at DESC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
        ).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["minutes"] = round(d["minutes"], 1) if d["minutes"] is not None else None
        out.append(RecentJob(**d))
    return out


@router.get("/failures", response_model=list[FailedJob])
async def failures(
    limit: int = Query(10, ge=1, le=50),
    user: AuthUser = Depends(get_current_user),
) -> list[FailedJob]:
    async with get_tenant_session(user.tenant_id) as session:
        rows = (
            await session.execute(
                text("""
                    SELECT j.id AS job_id, j.created_at,
                           COALESCE(s.spec_data->>'vertical', 'n/a') AS pack,
                           left(j.error_detail::text, 200) AS error
                    FROM jobs j JOIN application_specs s ON s.id = j.spec_id
                    WHERE j.status = 'failed'
                    ORDER BY j.created_at DESC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
        ).mappings().all()
    return [FailedJob(**dict(r)) for r in rows]


@router.get("/capacity", response_model=Capacity)
async def capacity(user: AuthUser = Depends(get_current_user)) -> Capacity:
    """Global generation-slot usage (not tenant-specific) from the capacity manager."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            data = (await client.get(f"{CAPACITY_URL}/status")).json()
    except Exception:
        return Capacity(available=False)
    qd = data.get("queue_depth") or {}
    try:
        queue_total = sum(int(v) for v in qd.values()) if isinstance(qd, dict) else 0
    except Exception:
        queue_total = 0
    return Capacity(
        available=True,
        llm_slots_used=data.get("llm_slots_used"),
        llm_slots_total=data.get("llm_slots_total"),
        build_slots_used=data.get("build_slots_used"),
        build_slots_total=data.get("build_slots_total"),
        queue_total=queue_total,
    )
