from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.config import get_settings

settings = get_settings()

# ---------------------------------------------------------------------------
# Engine — single shared instance per process (connection pool)
# ---------------------------------------------------------------------------
engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    # Only log SQL in dev so we can see every query during Phase 0 testing
    echo=(settings.API_ENV == "development"),
)

_SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Tenant-scoped session
# ---------------------------------------------------------------------------
@asynccontextmanager
async def get_tenant_session(tenant_id: UUID) -> AsyncGenerator[AsyncSession, None]:
    """
    Yields an AsyncSession with the RLS tenant context set for the current
    transaction. SET LOCAL means the setting expires when the transaction commits
    or rolls back — it never bleeds into the next request.

    Usage:
        async with get_tenant_session(user.tenant_id) as session:
            await session.execute(text("SELECT ..."))
    """
    async with _SessionFactory() as session:
        async with session.begin():
            # SET LOCAL does not accept parameterized values ($1 / :tid).
            # tenant_id is always a validated UUID so direct interpolation is safe.
            await session.execute(
                text(f"SET LOCAL {settings.TENANT_SETTING_KEY} = '{tenant_id!s}'")
            )
            yield session
            # session.begin() commits here on clean exit, rolls back on exc
