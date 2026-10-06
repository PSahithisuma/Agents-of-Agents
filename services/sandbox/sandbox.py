"""
Sandbox Service — Cloud Run
Triggers Cloud Build QA Gate per job using the REST API (avoids google-cloud-build SDK conflicts).

POST /run    → trigger Cloud Build for a job
GET  /report/{job_id} → fetch GateReport from Cloud Storage
GET  /health  → liveness
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Literal

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException
from google.auth import default as google_auth_default
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import storage
from pydantic import BaseModel

app = FastAPI(title="VibeForge Sandbox")

PROJECT_ID       = os.environ["PROJECT_ID"]
REGION           = os.environ.get("REGION", "us-central1")
ARTIFACTS_BUCKET = os.environ.get("ARTIFACTS_BUCKET", "vibeforge-artifacts-dev")
DATABASE_URL     = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)

STACK_CONFIGS: dict[str, str] = {
    "java_springboot": "java_springboot.yaml",
    "python_fastapi":  "python_fastapi.yaml",
}


# ── Pydantic Models ───────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    job_id:    str
    tenant_id: str
    stack:     Literal["java_springboot", "python_fastapi"]

class StepResult(BaseModel):
    name:       str
    passed:     bool
    duration_s: float
    detail:     dict = {}

class GateReport(BaseModel):
    job_id:       str
    tenant_id:    str
    stack:        str
    overall:      Literal["pass", "fail"]
    steps:        list[StepResult]
    coverage_pct: float | None = None
    sbom_url:     str | None   = None
    created_at:   datetime


# ── Database Pool ─────────────────────────────────────────────────────────────

pool: asyncpg.Pool | None = None

@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)

@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.close()


# ── GCP Auth Helper ───────────────────────────────────────────────────────────

def _get_access_token() -> str:
    creds, _ = google_auth_default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(GoogleAuthRequest())
    return creds.token


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "sandbox"}


@app.post("/run", status_code=202)
async def trigger_gate(req: RunRequest):
    """Trigger Cloud Build QA Gate for a job via REST API."""
    if req.stack not in STACK_CONFIGS:
        raise HTTPException(400, f"Unknown stack: {req.stack}")

    local_config = f"/app/cloudbuild/{STACK_CONFIGS[req.stack]}"
    if not os.path.exists(local_config):
        raise HTTPException(500, f"Cloudbuild config not found: {local_config}")

    # Upload cloudbuild YAML to GCS
    gcs = storage.Client()
    bucket = gcs.bucket(ARTIFACTS_BUCKET)
    config_blob = bucket.blob(f"cloudbuild/{req.job_id}/{req.stack}.yaml")
    config_blob.upload_from_filename(local_config, content_type="text/yaml")

    # Build request payload
    build_request = {
        "source": {
            "storageSource": {
                "bucket": ARTIFACTS_BUCKET,
                "object": f"artifacts/{req.job_id}/source.tar.gz",
            }
        },
        "filename": f"cloudbuild/{req.job_id}/{req.stack}.yaml",
        "substitutions": {
            "_JOB_ID":    req.job_id,
            "_TENANT_ID": req.tenant_id,
            "_STACK":     req.stack,
            "_REGION":    REGION,
            "_BUCKET":    ARTIFACTS_BUCKET,
        },
        "tags": [f"job-{req.job_id}", f"tenant-{req.tenant_id}", req.stack],
    }

    token = _get_access_token()
    resp = httpx.post(
        f"https://cloudbuild.googleapis.com/v1/projects/{PROJECT_ID}/builds",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=build_request,
        timeout=30.0,
    )
    if resp.status_code not in (200, 201):
        raise HTTPException(500, f"Cloud Build API error: {resp.text[:400]}")

    build_id = resp.json().get("metadata", {}).get("build", {}).get("id", "unknown")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL app.current_tenant_id = $1", req.tenant_id)
            await conn.execute(
                """
                INSERT INTO gate_runs (id, job_id, tenant_id, stack, build_id, status, created_at)
                VALUES ($1, $2, $3, $4, $5, 'running', NOW())
                ON CONFLICT DO NOTHING
                """,
                str(uuid.uuid4()), req.job_id, req.tenant_id, req.stack, build_id,
            )

    return {"build_id": build_id, "job_id": req.job_id, "status": "running"}


@app.get("/report/{job_id}")
async def get_gate_report(job_id: str):
    """Fetch GateReport from Cloud Storage."""
    gcs = storage.Client()
    bucket = gcs.bucket(ARTIFACTS_BUCKET)
    blob = bucket.blob(f"gate-runs/{job_id}/gate_report.json")
    if not blob.exists():
        raise HTTPException(404, "GateReport not found — build may still be running")
    return json.loads(blob.download_as_text())