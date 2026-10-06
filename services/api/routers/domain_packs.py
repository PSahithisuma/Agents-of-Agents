"""
Domain Packs Router — Custom YAML Pack Upload (Issue #3)
POST /api/v1/domain-packs/upload  — validate and register a custom YAML pack
GET  /api/v1/domain-packs         — list available packs
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import uuid
from typing import Any

import yaml
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from core.auth import AuthUser, get_current_user
from core.redis_client import get_redis

router = APIRouter(prefix="/api/v1/domain-packs", tags=["domain-packs"])

_REQUIRED_FIELDS = {"entities", "compliance", "framework"}
_MAX_SIZE_BYTES  = 512 * 1024  # 512 KB


class PackSummary(BaseModel):
    pack_id: str
    name: str
    entities: int
    compliance: str
    framework: str
    uploaded_by: str


def _validate_pack(data: dict[str, Any]) -> list[str]:
    errors = []
    missing = _REQUIRED_FIELDS - set(data.keys())
    if missing:
        errors.append(f"Missing required fields: {sorted(missing)}")
    if "entities" in data and not isinstance(data["entities"], int):
        errors.append("'entities' must be an integer (number of entities to generate)")
    if "entities" in data and isinstance(data["entities"], int):
        if not (1 <= data["entities"] <= 100):
            errors.append("'entities' must be between 1 and 100")
    return errors


@router.post("/upload", response_model=PackSummary, status_code=status.HTTP_201_CREATED)
async def upload_domain_pack(
    file: UploadFile = File(..., description="YAML domain pack definition"),
    user: AuthUser = Depends(get_current_user),
):
    """
    Upload and register a custom domain pack YAML file.

    The YAML must contain at minimum:
      - entities: <int>      # number of entities to generate
      - compliance: <str>    # e.g. "GDPR", "HIPAA", "ISO 27001"
      - framework: <str>     # e.g. "Spring Boot", "FastAPI"

    Optional fields: tables, package, entity, description, tags
    """
    if not file.filename or not file.filename.endswith((".yaml", ".yml")):
        raise HTTPException(status_code=422, detail="File must be a .yaml or .yml file")

    raw = await file.read()
    if len(raw) > _MAX_SIZE_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {_MAX_SIZE_BYTES // 1024}KB)")

    # Parse YAML
    try:
        data = yaml.safe_load(io.BytesIO(raw))
    except yaml.YAMLError as e:
        raise HTTPException(status_code=422, detail=f"Invalid YAML: {e}")

    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="YAML root must be a mapping (dict)")

    # Validate required fields
    errors = _validate_pack(data)
    if errors:
        raise HTTPException(status_code=422, detail={"validation_errors": errors})

    # Generate stable pack_id from content hash
    content_hash = hashlib.sha256(raw).hexdigest()[:8]
    pack_name = os.path.splitext(file.filename)[0].lower().replace(" ", "_")
    pack_id = f"custom-{pack_name}-{content_hash}"

    # Store in Redis (TTL: 30 days)
    redis = get_redis()
    redis_key = f"domainpack:{user.tenant_id}:{pack_id}"
    pack_record = {
        "pack_id": pack_id,
        "name": pack_name,
        "entities": int(data["entities"]),
        "compliance": str(data.get("compliance", "Custom")),
        "framework": str(data.get("framework", "Spring Boot")),
        "uploaded_by": user.email or str(user.tenant_id),
        "raw_yaml": raw.decode("utf-8", errors="replace"),
    }
    await redis.set(redis_key, json.dumps(pack_record), ex=60 * 60 * 24 * 30)

    return PackSummary(
        pack_id=pack_id,
        name=pack_name,
        entities=pack_record["entities"],
        compliance=pack_record["compliance"],
        framework=pack_record["framework"],
        uploaded_by=pack_record["uploaded_by"],
    )


@router.get("", response_model=list[PackSummary])
async def list_domain_packs(user: AuthUser = Depends(get_current_user)):
    """List all custom domain packs uploaded by this tenant."""
    redis = get_redis()
    pattern = f"domainpack:{user.tenant_id}:*"
    keys = await redis.keys(pattern)

    result = []
    for k in keys:
        raw = await redis.get(k)
        if raw:
            rec = json.loads(raw)
            result.append(PackSummary(**{f: rec[f] for f in PackSummary.model_fields}))

    return result
