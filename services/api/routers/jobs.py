from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from core.auth import AuthUser, get_current_user
from core.database import get_tenant_session

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class JobEventOut(BaseModel):
    seq: int
    event_type: str
    payload: dict
    created_at: datetime


class JobOut(BaseModel):
    id: UUID
    tenant_id: UUID
    spec_id: UUID
    status: str
    job_type: str
    idempotency_key: str
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/", response_model=list[JobOut])
async def list_jobs(
    user: AuthUser = Depends(get_current_user),
) -> list[JobOut]:
    """List the 50 most recent jobs for the authenticated tenant."""
    async with get_tenant_session(user.tenant_id) as session:
        rows = (
            await session.execute(
                text("""
                    SELECT id, tenant_id, spec_id, status, job_type, idempotency_key,
                           created_at, started_at, finished_at
                    FROM jobs
                    ORDER BY created_at DESC
                    LIMIT 50
                """),
            )
        ).mappings().all()

    return [JobOut(**r) for r in rows]


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: UUID,
    user: AuthUser = Depends(get_current_user),
) -> JobOut:
    """Get a single job by ID (RLS enforces tenant ownership)."""
    async with get_tenant_session(user.tenant_id) as session:
        row = (
            await session.execute(
                text("""
                    SELECT id, tenant_id, spec_id, status, job_type, idempotency_key,
                           created_at, started_at, finished_at
                    FROM jobs WHERE id = :id
                """),
                {"id": str(job_id)},
            )
        ).mappings().one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobOut(**row)


@router.get("/{job_id}/events", response_model=list[JobEventOut])
async def get_job_events(
    job_id: UUID,
    user: AuthUser = Depends(get_current_user),
) -> list[JobEventOut]:
    """Return all persisted events for a job in sequence order."""
    async with get_tenant_session(user.tenant_id) as session:
        # Verify job ownership first
        job = (
            await session.execute(
                text("SELECT id FROM jobs WHERE id = :id"),
                {"id": str(job_id)},
            )
        ).scalar_one_or_none()

        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")

        rows = (
            await session.execute(
                text("""
                    SELECT seq, event_type, payload, created_at
                    FROM job_events
                    WHERE job_id = :job_id
                    ORDER BY seq ASC
                """),
                {"job_id": str(job_id)},
            )
        ).mappings().all()

    return [JobEventOut(**r) for r in rows]
