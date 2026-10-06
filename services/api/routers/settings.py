"""
Settings Router — API Key Vault (Issue #3)
POST /api/v1/settings/keys       — store encrypted enterprise API key
GET  /api/v1/settings/keys       — list stored providers (masked)
DELETE /api/v1/settings/keys/{provider} — remove a key
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from core.auth import AuthUser, get_current_user
from core.redis_client import get_redis

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

Provider = Literal["openai", "anthropic", "gemini", "groq", "cohere"]

_SERVER_SECRET = os.environ.get("SETTINGS_SECRET", "vibeforge-vault-2026").encode()


def _encrypt(api_key: str) -> str:
    """XOR-mask with HMAC-derived key — lightweight, reversible server-side."""
    key_bytes = hashlib.sha256(_SERVER_SECRET).digest()
    data = api_key.encode()
    masked = bytes(b ^ key_bytes[i % 32] for i, b in enumerate(data))
    return base64.urlsafe_b64encode(masked).decode()


def _decrypt(token: str) -> str:
    key_bytes = hashlib.sha256(_SERVER_SECRET).digest()
    data = base64.urlsafe_b64decode(token.encode())
    return bytes(b ^ key_bytes[i % 32] for i, b in enumerate(data)).decode()


def _mask(api_key: str) -> str:
    if len(api_key) <= 8:
        return "****"
    return api_key[:4] + "****" + api_key[-4:]


class KeyPayload(BaseModel):
    provider: Provider
    api_key: str


class KeyResponse(BaseModel):
    provider: str
    masked_key: str
    stored: bool = True


@router.post("/keys", response_model=KeyResponse, status_code=status.HTTP_201_CREATED)
async def store_api_key(
    body: KeyPayload,
    user: AuthUser = Depends(get_current_user),
):
    """Store an encrypted enterprise API key for the authenticated tenant."""
    if len(body.api_key) < 8:
        raise HTTPException(status_code=422, detail="api_key too short")

    redis = get_redis()
    redis_key = f"apikey:{user.tenant_id}:{body.provider}"
    encrypted = _encrypt(body.api_key)
    await redis.set(redis_key, encrypted)

    return KeyResponse(
        provider=body.provider,
        masked_key=_mask(body.api_key),
    )


@router.get("/keys", response_model=list[KeyResponse])
async def list_api_keys(user: AuthUser = Depends(get_current_user)):
    """List all stored API key providers for this tenant (keys are masked)."""
    redis = get_redis()
    pattern = f"apikey:{user.tenant_id}:*"
    keys = await redis.keys(pattern)

    result = []
    for k in keys:
        provider = k.split(":")[-1]
        encrypted = await redis.get(k)
        if encrypted:
            decrypted = _decrypt(encrypted)
            result.append(KeyResponse(provider=provider, masked_key=_mask(decrypted)))

    return result


@router.delete("/keys/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_key(
    provider: Provider,
    user: AuthUser = Depends(get_current_user),
):
    """Remove a stored API key."""
    redis = get_redis()
    deleted = await redis.delete(f"apikey:{user.tenant_id}:{provider}")
    if not deleted:
        raise HTTPException(status_code=404, detail=f"No key stored for provider '{provider}'")
