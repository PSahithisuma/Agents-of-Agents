"""
routers/specs.py — Spec IR CRUD endpoints

POST /api/v1/projects/{id}/specs     → compile selections into AppSpec, save draft
GET  /api/v1/projects/{id}/specs     → list all spec versions for a project
GET  /api/v1/projects/{id}/specs/{v} → get a specific version (with canonical_hash)
"""
from __future__ import annotations
import os

import time
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text

from core.auth import AuthUser, get_current_user
from core.database import get_tenant_session
from core.ratelimit import check_rate
from core.metrics import SPEC_COMPILED, SPEC_COMPILE_DURATION
from domain_packs.v0.compiler import compile_spec
from domain_packs.v0.options import OPTION_GRAPH

async def require_writer(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    """Edit / lock / generate need the admin or developer role; viewers are read-only."""
    if not ({"admin", "developer"} & set(user.roles or [])):
        raise HTTPException(status_code=403, detail="requires admin or developer role")
    return user


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


# =============================================================================
# Spec lifecycle: edit a draft -> lock -> generate from the locked spec
# =============================================================================
import json as _json

from models.spec_edit import apply_entities, validate_entities


class EntitiesEdit(BaseModel):
    entities: list[dict]
    note: str = ""


def _spec_out(row) -> SpecOut:
    return SpecOut(
        id=row["id"], project_id=row["project_id"], tenant_id=row["tenant_id"], version=row["version"],
        canonical_hash=row["canonical_hash"], frozen_at=str(row["frozen_at"]) if row["frozen_at"] else None,
        spec_data=row["spec_data"], created_at=str(row["created_at"]),
    )


_SPEC_COLS = "id, project_id, tenant_id, version, canonical_hash, frozen_at, spec_data, created_at"


@router.patch(
    "/{project_id}/specs/{version}/entities",
    response_model=SpecOut,
    summary="Replace the extra entities of a DRAFT spec (validated); recomputes the canonical hash",
)
async def edit_spec_entities(project_id: UUID, version: int, body: EntitiesEdit,
                             user: AuthUser = Depends(require_writer)) -> SpecOut:
    try:
        entities = validate_entities(body.entities)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    async with get_tenant_session(user.tenant_id) as session:
        row = (await session.execute(
            text(f"SELECT {_SPEC_COLS} FROM application_specs WHERE project_id = :pid AND version = :v"),
            {"pid": str(project_id), "v": version},
        )).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Spec version not found")
        if row["frozen_at"] is not None:
            raise HTTPException(status_code=409, detail="Spec is locked; create a new draft version to change it")
        try:
            new_dict, new_hash = apply_entities(row["spec_data"], entities)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"cannot rebuild spec: {exc}")
        upd = (await session.execute(
            text(f"""UPDATE application_specs SET spec_data = cast(:sd as jsonb), canonical_hash = :h
                     WHERE id = :id AND frozen_at IS NULL RETURNING {_SPEC_COLS}"""),
            {"sd": _json.dumps(new_dict), "h": new_hash, "id": str(row["id"])},
        )).mappings().one_or_none()
        if upd is None:
            raise HTTPException(status_code=409, detail="Spec changed or was locked concurrently; retry")
        await session.execute(
            text("""INSERT INTO spec_provenance (tenant_id, spec_id, option_id, json_path, patch_data)
                    VALUES (:t, :s, :o, :p, cast(:d as jsonb))"""),
            {"t": str(user.tenant_id), "s": str(row["id"]), "o": "user_edit",
             "p": "domain_model.entities",
             "d": _json.dumps({"by": str(user.id), "note": body.note, "entities": [e["name"] for e in entities],
                               "hash_before": row["canonical_hash"], "hash_after": new_hash})},
        )
    return _spec_out(upd)


@router.post(
    "/{project_id}/specs/{version}/lock",
    response_model=SpecOut,
    summary="Lock a draft spec (sets frozen_at). A locked spec is immutable.",
)
async def lock_spec(project_id: UUID, version: int, user: AuthUser = Depends(require_writer)) -> SpecOut:
    async with get_tenant_session(user.tenant_id) as session:
        row = (await session.execute(
            text(f"SELECT {_SPEC_COLS} FROM application_specs WHERE project_id = :pid AND version = :v"),
            {"pid": str(project_id), "v": version},
        )).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Spec version not found")
        if row["frozen_at"] is not None:
            return _spec_out(row)          # already locked: idempotent
        upd = (await session.execute(
            text(f"UPDATE application_specs SET frozen_at = NOW() WHERE id = :id AND frozen_at IS NULL RETURNING {_SPEC_COLS}"),
            {"id": str(row["id"])},
        )).mappings().one_or_none()
        if upd is None:
            raise HTTPException(status_code=409, detail="Spec changed concurrently; retry")
        await session.execute(
            text("""INSERT INTO spec_provenance (tenant_id, spec_id, option_id, json_path, patch_data)
                    VALUES (:t, :s, 'lock', '$', cast(:d as jsonb))"""),
            {"t": str(user.tenant_id), "s": str(row["id"]),
             "d": _json.dumps({"by": str(user.id), "hash": row["canonical_hash"]})},
        )
    return _spec_out(upd)


class SpecGenerateOut(BaseModel):
    job_id: UUID
    project_id: UUID
    spec_version: int
    canonical_hash: Optional[str]
    stream_url: str
    cache_hit: bool = False


@router.post(
    "/{project_id}/specs/{version}/generate",
    response_model=SpecGenerateOut,
    summary="Generate from a LOCKED spec version (no recompilation)",
)
async def generate_from_spec(project_id: UUID, version: int, request: Request, use_cache: bool = False,
                             user: AuthUser = Depends(require_writer)) -> SpecGenerateOut:
    base_url = (os.environ.get("PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")
    await check_rate(str(user.tenant_id), "generate", int(os.environ.get("RATE_GENERATE_PER_HOUR", "30")), 3600)
    async with get_tenant_session(user.tenant_id) as session:
        row = (await session.execute(
            text(f"SELECT {_SPEC_COLS} FROM application_specs WHERE project_id = :pid AND version = :v"),
            {"pid": str(project_id), "v": version},
        )).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Spec version not found")
        if row["frozen_at"] is None:
            raise HTTPException(status_code=409, detail="Lock the spec first: POST .../specs/{version}/lock")
        _left = (await session.execute(
            text("SELECT budget_usd - spent_usd FROM budgets ORDER BY period_end DESC NULLS LAST LIMIT 1"))).scalar_one_or_none()
        if _left is not None and float(_left) <= 0:
            raise HTTPException(status_code=402, detail="tenant budget exhausted; raise the budget to generate")
        if use_cache:
            _hit = (await session.execute(
                text("""SELECT job_id FROM cache_entries WHERE canonical_hash = :h AND stack_profile = 'java_spring'
                        AND scaffold_version = :sv AND ruleset_version = :rv ORDER BY created_at DESC LIMIT 1"""),
                {"h": row["canonical_hash"], "sv": os.environ.get("SCAFFOLD_VERSION", "1.0.0"),
                 "rv": os.environ.get("RULESET_VERSION", "1.0.0")},
            )).scalar_one_or_none()
            if _hit is not None:
                return SpecGenerateOut(job_id=_hit, project_id=project_id, spec_version=version,
                                       canonical_hash=row["canonical_hash"],
                                       stream_url=f"{base_url}/api/v1/jobs/{_hit}/stream", cache_hit=True)
        live = (await session.execute(
            text("""SELECT id FROM jobs WHERE spec_id = :s AND status NOT IN ('failed','cancelled')
                    ORDER BY created_at DESC LIMIT 1"""),
            {"s": str(row["id"])},
        )).scalar_one_or_none()
        if live is not None:
            return SpecGenerateOut(job_id=live, project_id=project_id, spec_version=version,
                                   canonical_hash=row["canonical_hash"],
                                   stream_url=f"{base_url}/api/v1/jobs/{live}/stream", cache_hit=True)
        n = (await session.execute(text("SELECT COUNT(*) FROM jobs WHERE spec_id = :s"), {"s": str(row["id"])})).scalar()
        ikey = f"gen-{row['id']}-r{n + 1}"
        job_id = (await session.execute(
            text("""INSERT INTO jobs (tenant_id, spec_id, idempotency_key, job_type, status)
                    VALUES (:t, :s, :k, 'generation', 'queued')
                    ON CONFLICT (idempotency_key) DO UPDATE SET status = jobs.status RETURNING id"""),
            {"t": str(user.tenant_id), "s": str(row["id"]), "k": ikey},
        )).scalar_one()
    await request.app.state.arq_pool.enqueue_job(
        "run_generation", _job_id=ikey, job_id=str(job_id), tenant_id=str(user.tenant_id), spec_data=row["spec_data"],
    )
    return SpecGenerateOut(job_id=job_id, project_id=project_id, spec_version=version,
                           canonical_hash=row["canonical_hash"], stream_url=f"{base_url}/api/v1/jobs/{job_id}/stream")
