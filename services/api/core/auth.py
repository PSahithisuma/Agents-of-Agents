from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import UUID

import httpx
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import text

from core.config import get_settings
from core.database import get_tenant_session

settings = get_settings()
_bearer = HTTPBearer(auto_error=True)

# Dev-mode seed tenant — matches SEED_TENANT_ID in config
_DEV_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# AuthUser — the principal injected into every protected route
# ---------------------------------------------------------------------------

@dataclass
class AuthUser:
    id: UUID           # vibeforge users.id (DB PK)
    tenant_id: UUID    # from JWT claim
    keycloak_sub: str  # JWT sub — stable Keycloak user UUID
    email: str
    roles: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# JWKS cache — in-process, refreshed every hour
# On key rotation, the kid-not-found path forces an immediate refresh.
# ---------------------------------------------------------------------------

_jwks_cache: dict | None = None
_jwks_fetched_at: float = 0.0
_JWKS_TTL: int = 3600  # 1 hour


async def _fetch_jwks() -> dict:
    global _jwks_cache, _jwks_fetched_at
    now = time.monotonic()
    if _jwks_cache is None or (now - _jwks_fetched_at) > _JWKS_TTL:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(settings.KEYCLOAK_JWKS_URL)
            resp.raise_for_status()
            _jwks_cache = resp.json()
            _jwks_fetched_at = now
    return _jwks_cache


def _find_rsa_key(jwks: dict, kid: str | None) -> dict | None:
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return {k: key[k] for k in ("kty", "kid", "n", "e") if k in key}
    return None


async def _decode_token(token: str) -> dict:
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))

    kid = header.get("kid")
    jwks = await _fetch_jwks()
    rsa_key = _find_rsa_key(jwks, kid)

    if rsa_key is None:
        # kid not in cache — Keycloak may have rotated keys; force one refresh
        global _jwks_cache
        _jwks_cache = None
        jwks = await _fetch_jwks()
        rsa_key = _find_rsa_key(jwks, kid)

    if rsa_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"No JWK matching kid='{kid}' found in Keycloak JWKS",
        )

    try:
        return jwt.decode(
            token,
            rsa_key,
            algorithms=["RS256"],
            options={"verify_aud": False},  # VibeForge doesn't use aud in Phase 0
        )
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))


# ---------------------------------------------------------------------------
# FastAPI dependency — validate JWT → upsert user → return AuthUser
# ---------------------------------------------------------------------------

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> AuthUser:
    """
    1. Validate RS256 JWT from Keycloak.
    2. Extract tenant_id, sub, roles from claims.
    3. Upsert user row in DB (create on first login, update role/email on next).
    4. Return AuthUser injected into every protected route handler.

    DEV MODE: When API_ENV=development, accepts token format:
      "dev:{email}"                — uses seed tenant, role=admin
      "dev:{tenant_id}:{email}"   — uses specified tenant UUID, role=admin
    """
    token = credentials.credentials

    # ── Dev bypass ──────────────────────────────────────────────────────────
    if settings.API_ENV == "development" and token.startswith("dev:"):
        parts = token.split(":", 2)
        if len(parts) == 2:
            # dev:{email}
            dev_email = parts[1]
            dev_tenant = _DEV_TENANT_ID
        elif len(parts) == 3:
            # dev:{tenant_id}:{email}
            try:
                dev_tenant = UUID(parts[1])
            except ValueError:
                dev_tenant = _DEV_TENANT_ID
            dev_email = parts[2]
        else:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Invalid dev token format")

        dev_sub = f"dev-{dev_email}"
        async with get_tenant_session(dev_tenant) as session:
            result = await session.execute(
                text("""
                    INSERT INTO users (tenant_id, keycloak_sub, email, role)
                    VALUES (:tenant_id, :sub, :email, 'admin')
                    ON CONFLICT (keycloak_sub) DO UPDATE SET
                        email      = EXCLUDED.email,
                        updated_at = NOW()
                    RETURNING id
                """),
                {"tenant_id": str(dev_tenant), "sub": dev_sub, "email": dev_email},
            )
            user_id: UUID = result.scalar_one()

        return AuthUser(
            id=user_id,
            tenant_id=dev_tenant,
            keycloak_sub=dev_sub,
            email=dev_email,
            roles=["admin"],
        )
    # ── End dev bypass ───────────────────────────────────────────────────────

    payload = await _decode_token(credentials.credentials)

    sub: str | None = payload.get("sub")
    tenant_id_str: str | None = payload.get("tenant_id")
    roles: list[str] = payload.get("roles", [])
    email: str = payload.get("email") or payload.get("preferred_username") or sub or ""

    if not sub or not tenant_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing required claims: sub, tenant_id",
        )

    try:
        tenant_id = UUID(tenant_id_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="tenant_id claim is not a valid UUID",
        )

    # Map Keycloak roles → app role (highest privilege wins)
    app_role = "viewer"
    if "admin" in roles:
        app_role = "admin"
    elif "developer" in roles:
        app_role = "developer"

    # Upsert — creates user on first API call, keeps email/role fresh
    async with get_tenant_session(tenant_id) as session:
        result = await session.execute(
            text("""
                INSERT INTO users (tenant_id, keycloak_sub, email, role)
                VALUES (:tenant_id, :sub, :email, :role)
                ON CONFLICT (keycloak_sub) DO UPDATE SET
                    email      = EXCLUDED.email,
                    role       = EXCLUDED.role,
                    updated_at = NOW()
                RETURNING id
            """),
            {
                "tenant_id": str(tenant_id),
                "sub": sub,
                "email": email,
                "role": app_role,
            },
        )
        user_id: UUID = result.scalar_one()

    return AuthUser(
        id=user_id,
        tenant_id=tenant_id,
        keycloak_sub=sub,
        email=email,
        roles=roles,
    )
