"""
routers/specs.py — Spec IR CRUD endpoints

POST /api/v1/projects/{id}/specs     → compile selections into AppSpec, save draft
GET  /api/v1/projects/{id}/specs     → list all spec versions for a project
GET  /api/v1/projects/{id}/specs/{v} → get a specific version (with canonical_hash)
"""
from __future__ import annotations

import time
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text

from core.auth import AuthUser, get_current_user
from core.database import get_tenant_session
from core.metrics import SPEC_COMPILED, SPEC_COMPILE_DURATION
from domain_packs.v0.compiler import compile_spec
from domain_packs.v0.options import OPTION_GRAPH

router = APIRouter(prefix="/api/v1/projects", tags=["specs"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SpecDraftRequest(BaseModel):
    """
    Selections from the option-graph UI.
    Only the 8 v0 option keys are accepted; extras are silently ignored.
    Missing keys get their defaults from OPTION_GRAPH.
    """
    selections: dict[str, str] = {}
    app_name: str = ""
    description: str = ""
    vertical: str = ""


class SpecOut(BaseModel):
    id: UUID
    project_id: UUID
    tenant_id: UUID
    version: int
    canonical_hash: Optional[str]
    frozen_at: Optional[str]
    spec_data: dict
    created_at: str


# ---------------------------------------------------------------------------
# Helper: show available options (dev convenience)
# ---------------------------------------------------------------------------

@router.get("/option-graph", tags=["specs"], summary="List all available option keys and their choices")
async def get_option_graph() -> dict:
    return {
        key: {
            "label": opt.label,
            "choices": opt.choices,
            "default": opt.default,
            "description": opt.description,
        }
        for key, opt in OPTION_GRAPH.items()
    }


# ---------------------------------------------------------------------------
# POST — compile selections into a draft AppSpec and save
# ---------------------------------------------------------------------------

@router.post(
    "/{project_id}/specs/",
    response_model=SpecOut,
    status_code=status.HTTP_201_CREATED,
    summary="Compile option selections into a draft AppSpec",
)
async def create_spec(
    project_id: UUID,
    body: SpecDraftRequest,
    user: AuthUser = Depends(get_current_user),
) -> SpecOut:
    """
    1. Validates selections against the option graph (fills defaults for missing keys).
    2. Compiles a deterministic AppSpec with canonical_hash.
    3. Saves as a new draft version (frozen_at = NULL).
    4. Returns the spec row including canonical_hash.

    The canonical_hash can be used by clients to check for cache hits before
    calling /generate — if the hash is already in the DB, a job exists.
    """
    # Compile AppSpec from selections
    t0 = time.monotonic()
    vertical = (body.vertical or "").strip().lower() or "generic"
    if vertical not in ("generic", "banking", "ecommerce", "logistics", "custom"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"unknown vertical {vertical!r}")
    try:
        spec = compile_spec(
            body.selections,
            app_name=body.app_name,
            description=body.description,
            vertical=vertical,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    elapsed = time.monotonic() - t0

    # Track metrics
    SPEC_COMPILE_DURATION.observe(elapsed)
    SPEC_COMPILED.labels(
        ui_framework=spec.ui_model.framework,
        db_tier=spec.nfr.db_tier,
        auth_strategy=spec.security.auth_strategy,
    ).inc()

    spec_dict = spec.freeze()   # includes canonical_hash

    async with get_tenant_session(user.tenant_id) as session:
        # Verify project belongs to this tenant
        proj = (await session.execute(
            text("SELECT id FROM projects WHERE id = :id"),
            {"id": str(project_id)},
        )).scalar_one_or_none()

        if proj is None:
            raise HTTPException(status_code=404, detail="Project not found")

        # Determine next version number
        current_max = (await session.execute(
            text("SELECT COALESCE(MAX(version), 0) FROM application_specs WHERE project_id = :pid"),
            {"pid": str(project_id)},
        )).scalar()

        next_version = current_max + 1

        row = (await session.execute(
            text("""
                INSERT INTO application_specs
                    (tenant_id, project_id, created_by, version, spec_data, canonical_hash)
                VALUES
                    (:tenant_id, :project_id, :created_by, :version,
                     cast(:spec_data as jsonb), :canonical_hash)
                RETURNING id, project_id, tenant_id, version, canonical_hash,
                          frozen_at, spec_data, created_at
            """),
            {
                "tenant_id":      str(user.tenant_id),
                "project_id":     str(project_id),
                "created_by":     str(user.id),
                "version":        next_version,
                "spec_data":      __import__("json").dumps(spec_dict),
                "canonical_hash": spec.canonical_hash,
            },
        )).mappings().one()

    return SpecOut(
        id=row["id"],
        project_id=row["project_id"],
        tenant_id=row["tenant_id"],
        version=row["version"],
        canonical_hash=row["canonical_hash"],
        frozen_at=str(row["frozen_at"]) if row["frozen_at"] else None,
        spec_data=row["spec_data"],
        created_at=str(row["created_at"]),
    )


# ---------------------------------------------------------------------------
# GET — list spec versions for a project
# ---------------------------------------------------------------------------

@router.get(
    "/{project_id}/specs/",
    response_model=list[SpecOut],
    summary="List all spec versions for a project",
)
async def list_specs(
    project_id: UUID,
    user: AuthUser = Depends(get_current_user),
) -> list[SpecOut]:
    async with get_tenant_session(user.tenant_id) as session:
        rows = (await session.execute(
            text("""
                SELECT id, project_id, tenant_id, version, canonical_hash,
                       frozen_at, spec_data, created_at
                FROM application_specs
                WHERE project_id = :pid
                ORDER BY version DESC
            """),
            {"pid": str(project_id)},
        )).mappings().all()

    return [
        SpecOut(
            id=r["id"],
            project_id=r["project_id"],
            tenant_id=r["tenant_id"],
            version=r["version"],
            canonical_hash=r["canonical_hash"],
            frozen_at=str(r["frozen_at"]) if r["frozen_at"] else None,
            spec_data=r["spec_data"],
            created_at=str(r["created_at"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# GET — get a specific version
# ---------------------------------------------------------------------------

@router.get(
    "/{project_id}/specs/{version}/",
    response_model=SpecOut,
    summary="Get a specific spec version",
)
async def get_spec(
    project_id: UUID,
    version: int,
    user: AuthUser = Depends(get_current_user),
) -> SpecOut:
    async with get_tenant_session(user.tenant_id) as session:
        row = (await session.execute(
            text("""
                SELECT id, project_id, tenant_id, version, canonical_hash,
                       frozen_at, spec_data, created_at
                FROM application_specs
                WHERE project_id = :pid AND version = :ver
            """),
            {"pid": str(project_id), "ver": version},
        )).mappings().one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail=f"Spec version {version} not found")

    return SpecOut(
        id=row["id"],
        project_id=row["project_id"],
        tenant_id=row["tenant_id"],
        version=row["version"],
        canonical_hash=row["canonical_hash"],
        frozen_at=str(row["frozen_at"]) if row["frozen_at"] else None,
        spec_data=row["spec_data"],
        created_at=str(row["created_at"]),
    )
