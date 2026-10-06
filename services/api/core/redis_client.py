from __future__ import annotations

import redis.asyncio as aioredis

from core.config import get_settings

_pool: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """
    Return the shared async Redis client.
    Lazy-initialised on first call. The pool is reused for the process lifetime.
    Used for: pub/sub (SSE streaming) and general key operations.
    Note: Arq has its own internal Redis connection (configured via RedisSettings).
    """
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            max_connections=200,
        )
    return _pool


async def close_redis() -> None:
    """Graceful shutdown — called from app lifespan."""
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
