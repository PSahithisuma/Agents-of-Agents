from __future__ import annotations

import json
import zipfile
import io
import os
from typing import Optional
from uuid import UUID

from fastapi import Body, APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text

from core.auth import AuthUser, get_current_user
from core.database import get_tenant_session
from core.metrics import JOBS_ENQUEUED, JOBS_CACHE_HITS, PROJECTS_CREATED
from domain_packs.v0.compiler import compile_spec

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None


class ProjectOut(BaseModel):
    id: UUID
    name: str
    description: Optional[str]
    tenant_id: UUID


VALID_PACKS = ("banking", "ecommerce", "logistics", "custom")
ALLOWED_VERTICALS = {"generic", *VALID_PACKS}


# Keep in sync with resolve_pack in services/worker/tasks/generation.py (separate image).
def resolve_pack(spec):
    v = (spec.get("vertical") or "").strip().lower()
    if v in VALID_PACKS:
        return v
    name = (spec.get("app_name") or "").lower()
    description = spec.get("description") or ""
    if not name:
        raise HTTPException(status_code=422, detail="cannot determine pack: send vertical or app_name")
    if any(x in name for x in ["store", "commerce", "omni", "shop"]):
        return "ecommerce"
    if any(x in name for x in ["logi", "fleet", "route", "dispatch"]):
        return "logistics"
    if len(description) > 20 and not any(x in name for x in ["bank", "vibe"]):
        return "custom"
    return "banking"


class SpecSelectionsIn(BaseModel):
    """Option-graph selections for Phase 1 compile_spec. All keys optional — defaults used for omitted keys."""
    selections: dict[str, str] = {}
    app_name: str = ""
    description: str = ""
    vertical: str = ""


class GenerateOut(BaseModel):
    job_id: UUID
    project_id: UUID
    idempotency_key: str
    status: str = "queued"
    stream_url: str
    canonical_hash: str
    cache_hit: bool = False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate,
    user: AuthUser = Depends(get_current_user),
) -> ProjectOut:
    """Create a new project under the authenticated tenant."""
    async with get_tenant_session(user.tenant_id) as session:
        row = (
            await session.execute(
                text("""
                    INSERT INTO projects (tenant_id, owner_id, name, description)
                    VALUES (:tenant_id, :owner_id, :name, :description)
                    RETURNING id, name, description, tenant_id
                """),
                {
                    "tenant_id": str(user.tenant_id),
                    "owner_id": str(user.id),
                    "name": body.name,
                    "description": body.description,
                },
            )
        ).mappings().one()

    PROJECTS_CREATED.inc()
    return ProjectOut(**row)


@router.get("/", response_model=list[ProjectOut])
async def list_projects(
    user: AuthUser = Depends(get_current_user),
) -> list[ProjectOut]:
    """List all projects for the authenticated tenant (RLS enforced)."""
    async with get_tenant_session(user.tenant_id) as session:
        rows = (
            await session.execute(
                text("""
                    SELECT id, name, description, tenant_id
                    FROM projects
                    ORDER BY created_at DESC
                    LIMIT 100
                """),
            )
        ).mappings().all()

    return [ProjectOut(**r) for r in rows]




@router.get("/{project_id}/jobs/{job_id}/download", summary="Download generated project as ZIP")
async def download_project_zip(
    project_id: UUID,
    job_id: str,
    user: AuthUser = Depends(get_current_user),
):
    from fastapi.responses import StreamingResponse
    import zipfile, io, os

    import httpx as _httpx, base64 as _b64

    GITEA_URL = "http://gitea:3000"
    GITEA_TOKEN = os.environ["GITEA_TOKEN"]
    
    # Detect pack from job_id prefix by querying Gitea repos
    pack = "banking"
    for p in ["banking","ecommerce","logistics","custom"]:
        repo_name = f"agents-{p}-{job_id[:8]}"
        async with _httpx.AsyncClient(timeout=10) as _c:
            r = await _c.get(f"{GITEA_URL}/api/v1/repos/vibeforge-admin/{repo_name}",
                headers={"Authorization": f"token {GITEA_TOKEN}"})
            if r.status_code == 200:
                pack = p
                break

    repo_name = f"agents-{pack}-{job_id[:8]}"
    
    # Fetch all files from Gitea
    zip_buffer = io.BytesIO()
    async with _httpx.AsyncClient(timeout=30) as _c:
        # Get file tree
        tree_r = await _c.get(
            f"{GITEA_URL}/api/v1/repos/vibeforge-admin/{repo_name}/git/trees/main?recursive=true",
            headers={"Authorization": f"token {GITEA_TOKEN}"})
        
        if tree_r.status_code != 200:
            raise HTTPException(status_code=404, detail=f"Repo {repo_name} not found in Gitea")
        
        tree = tree_r.json().get("tree", [])
        
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in tree:
                if item.get("type") == "blob":
                    file_path = item["path"]
                    content_r = await _c.get(
                        f"{GITEA_URL}/api/v1/repos/vibeforge-admin/{repo_name}/contents/{file_path}",
                        headers={"Authorization": f"token {GITEA_TOKEN}"})
                    if content_r.status_code == 200:
                        file_content = _b64.b64decode(content_r.json()["content"])
                        zf.writestr(file_path, file_content)
    
    zip_buffer.seek(0)
    filename = f"agents-of-agents-{job_id[:8]}.zip"
    
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@router.post("/{project_id}/preview", summary="Preview generation plan before executing")
async def preview_generation(
    project_id: UUID,
    body: dict = Body(default={}),
    user: AuthUser = Depends(get_current_user),
):
    name = (body.get("app_name") or "").lower()
    description = body.get("description") or ""

    pack = resolve_pack(body)

    PACKS = {
        "banking":   {"compliance":"RBI Tier-1","tables":24,"entity":"BankAccount","package":"com.vibebank"},
        "ecommerce": {"compliance":"GDPR","tables":28,"entity":"Product","package":"com.omnistore"},
        "logistics": {"compliance":"ISO 27001","tables":18,"entity":"FleetVehicle","package":"com.logiroute"},
        "custom":    {"compliance":"Custom","tables":20,"entity":"CustomEntity","package":"com.customapp"},
    }
    cfg = PACKS[pack]
    pkg = cfg["package"].replace(".", "/")
    e = cfg["entity"]
    return {
        "pack": pack,
        "compliance": cfg["compliance"],
        "entity_class": e,
        "package": cfg["package"],
        "tables": cfg["tables"],
        "files_to_generate": [
            "pom.xml",
            f"src/main/java/{pkg}/entity/{e}.java",
            f"src/main/java/{pkg}/repository/{e}Repository.java",
            f"src/main/java/{pkg}/service/{e}Service.java",
            f"src/main/java/{pkg}/controller/{e}Controller.java",
            "src/main/resources/application.yml",
            "src/main/resources/db/migration/V1__create_tables.sql",
            "ERD.md"
        ],
        "estimated_seconds": 15,
        "ready": True,
        "message": f"Ready to generate 8 files — {cfg['compliance']} compliant Spring Boot project"
    }

@router.post(
    "/{project_id}/generate",
    response_model=GenerateOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Freeze latest spec draft and enqueue a generation job",
)
async def enqueue_generation(
    project_id: UUID,
    request: Request,
    body: SpecSelectionsIn = SpecSelectionsIn(),
    user: AuthUser = Depends(get_current_user),
) -> GenerateOut:
    """
    Phase 1: Compile option selections → AppSpec → freeze → enqueue job.

    Semantic cache (P3):
      If canonical_hash already exists for this tenant, returns the
      existing job_id immediately with cache_hit=True — no new job created.

    Backward compatible: if no selections provided, uses all defaults.
    """
    # ── 1. Compile AppSpec from option selections (defaults if none provided)
    vertical = (body.vertical or "").strip().lower() or "generic"
    if vertical not in ALLOWED_VERTICALS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown vertical {vertical!r}; expected one of {sorted(ALLOWED_VERTICALS)}",
        )
    try:
        app_spec = compile_spec(
            body.selections,
            app_name=body.app_name,
            description=body.description,
            vertical=vertical,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    canonical_hash = app_spec.canonical_hash

    async with get_tenant_session(user.tenant_id) as session:
        # 404 if project not in this tenant (RLS also blocks it, but 404 > 403)
        project = (
            await session.execute(
                text("SELECT id FROM projects WHERE id = :id"),
                {"id": str(project_id)},
            )
        ).scalar_one_or_none()

        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        # ── 2. Semantic cache check (P3) ─────────────────────────────────
        # If a spec with this exact canonical_hash already has a completed/running
        # job for this tenant, return it immediately — no redundant generation.
        cached = (
            await session.execute(
                text("""
                    SELECT j.id AS job_id, j.idempotency_key
                    FROM application_specs s
                    JOIN jobs j ON j.spec_id = s.id
                    WHERE s.tenant_id = :tenant_id
                      AND s.project_id = :project_id
                      AND s.canonical_hash = :chash
                      AND j.status NOT IN ('failed', 'cancelled')
                    ORDER BY j.created_at DESC
                    LIMIT 1
                """),
                {"tenant_id": str(user.tenant_id), "project_id": str(project_id), "chash": canonical_hash},
            )
        ).mappings().one_or_none()

        if cached:
            JOBS_CACHE_HITS.labels(tenant_id=str(user.tenant_id)).inc()
            base_url = str(request.base_url).rstrip("/")
            return GenerateOut(
                job_id=cached["job_id"],
                project_id=project_id,
                idempotency_key=cached["idempotency_key"],
                stream_url=f"{base_url}/api/v1/jobs/{cached['job_id']}/stream",
                canonical_hash=canonical_hash,
                cache_hit=True,
            )

        # ── 3. No cache hit — freeze spec and insert job row ─────────────
        spec_dict = app_spec.freeze()

        # Determine next version for this project
        current_max = (await session.execute(
            text("SELECT COALESCE(MAX(version), 0) FROM application_specs WHERE project_id = :pid"),
            {"pid": str(project_id)},
        )).scalar()

        spec_id = (
            await session.execute(
                text("""
                    INSERT INTO application_specs
                        (tenant_id, project_id, created_by, version,
                         spec_data, canonical_hash, frozen_at)
                    VALUES
                        (:tenant_id, :project_id, :created_by, :version,
                         cast(:spec_data as jsonb), :canonical_hash, NOW())
                    RETURNING id
                """),
                {
                    "tenant_id":      str(user.tenant_id),
                    "project_id":     str(project_id),
                    "created_by":     str(user.id),
                    "version":        current_max + 1,
                    "spec_data":      json.dumps(spec_dict),
                    "canonical_hash": canonical_hash,
                },
            )
        ).scalar_one()

        idempotency_key = f"gen-{spec_id}-v{current_max + 1}"

        job_id = (
            await session.execute(
                text("""
                    INSERT INTO jobs (tenant_id, spec_id, idempotency_key, job_type, status)
                    VALUES (:tenant_id, :spec_id, :ikey, 'generation', 'queued')
                    ON CONFLICT (idempotency_key) DO UPDATE SET status = jobs.status
                    RETURNING id
                """),
                {
                    "tenant_id": str(user.tenant_id),
                    "spec_id":   str(spec_id),
                    "ikey":      idempotency_key,
                },
            )
        ).scalar_one()

    # ── 4. Enqueue Arq job (_job_id = idempotency_key prevents double-dispatch)
    await request.app.state.arq_pool.enqueue_job(
        "run_generation",
        _job_id=idempotency_key,
        job_id=str(job_id),
        tenant_id=str(user.tenant_id),
        spec_data=spec_dict,
    )

    JOBS_ENQUEUED.labels(job_type="generation").inc()

    base_url = str(request.base_url).rstrip("/")
    return GenerateOut(
        job_id=job_id,
        project_id=project_id,
        idempotency_key=idempotency_key,
        stream_url=f"{base_url}/api/v1/jobs/{job_id}/stream",
        canonical_hash=canonical_hash,
        cache_hit=False,
    )

