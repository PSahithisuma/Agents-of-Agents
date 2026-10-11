import os, time
import redis.asyncio as aioredis
from fastapi import HTTPException

_pool = None


def _r():
    global _pool
    if _pool is None:
        pw = os.environ.get("REDIS_PASSWORD", "")
        auth = f":{pw}@" if pw else ""
        _pool = aioredis.from_url(f"redis://{auth}{os.environ.get('REDIS_HOST', 'localhost')}:{os.environ.get('REDIS_PORT', '6379')}",
                                  decode_responses=True, max_connections=5, socket_timeout=2, socket_connect_timeout=2)
    return _pool


async def check_rate(tenant_id: str, bucket: str, limit: int, window_s: int):
    try:
        k = f"rl:{bucket}:{tenant_id}:{int(time.time()) // window_s}"
        n = await _r().incr(k)
        if n == 1:
            await _r().expire(k, window_s + 5)
    except Exception:
        return
    if n > limit:
        raise HTTPException(status_code=429, detail=f"rate limit exceeded: {limit} {bucket} requests per {window_s}s",
                            headers={"Retry-After": str(window_s)})
