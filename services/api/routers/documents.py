"""Document upload for RAG.

Files go to MinIO as {tenant_id}/{filename}. The ingestion service polls the bucket
and indexes new files, so a user's documents are only ever searched for their own tenant.
"""
from __future__ import annotations

import asyncio
import io
import os
import re

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from minio import Minio

from core.auth import AuthUser, get_current_user
from routers.specs import require_writer
from core.breaker import Breaker

_BRK = Breaker('minio')


async def _guarded(fn):
    if _BRK.is_open:
        raise HTTPException(status_code=503, detail='Document storage is temporarily unavailable', headers={'Retry-After': str(_BRK.retry_after())})
    try:
        out = await asyncio.to_thread(fn)
    except Exception:
        _BRK.fail()
        raise HTTPException(status_code=503, detail='Document storage is temporarily unavailable', headers={'Retry-After': '30'})
    _BRK.ok()
    return out

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])

_BUCKET = os.environ.get("MINIO_BUCKET", "vf-docs")
_MAX_BYTES = 25 * 1024 * 1024
_ALLOWED = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".md", ".txt"}
_client: Minio | None = None


def _minio() -> Minio:
    global _client
    if _client is None:
        c = Minio(
            os.environ.get("MINIO_ENDPOINT", "minio:9000"),
            access_key=os.environ.get("MINIO_USER", ""),
            secret_key=os.environ.get("MINIO_PASSWORD", ""),
            secure=False,
        )
        if not c.bucket_exists(_BUCKET):
            c.make_bucket(_BUCKET)
        _client = c
    return _client


def _safe_name(name: str | None) -> str:
    base = os.path.basename(name or "").strip()
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base)
    return base[:120]


@router.post("", status_code=201)
async def upload_document(file: UploadFile = File(...), user: AuthUser = Depends(require_writer)):
    name = _safe_name(file.filename)
    ext = os.path.splitext(name)[1].lower()
    if not name or ext not in _ALLOWED:
        raise HTTPException(status_code=415, detail="Allowed types: " + ", ".join(sorted(_ALLOWED)))
    data = await file.read(_MAX_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="File larger than 25 MB")
    key = f"{user.tenant_id}/{name}"

    def _put():
        _minio().put_object(_BUCKET, key, io.BytesIO(data), len(data),
                            content_type=file.content_type or "application/octet-stream")

    await _guarded(_put)
    return {"filename": name, "bytes": len(data), "status": "uploaded; indexing starts within about 30 seconds"}


@router.get("")
async def list_documents(user: AuthUser = Depends(get_current_user)):
    prefix = f"{user.tenant_id}/"

    def _list():
        return [
            {"filename": o.object_name[len(prefix):], "bytes": o.size,
             "uploaded": o.last_modified.isoformat() if o.last_modified else None}
            for o in _minio().list_objects(_BUCKET, prefix=prefix)
        ]

    return {"documents": await _guarded(_list)}
