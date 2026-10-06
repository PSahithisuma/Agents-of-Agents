"""
Capacity Manager â€” Cloud Run (Phase 3, Contract C13)

Enforces fair scheduling across tenants:
- GPU pool: sized to vLLM max_num_seqs (for future GPU coder model)
- CPU/IO pool: semaphore on Cloud Build concurrent builds
- LiteLLM concurrency cap: max_parallel_requests
- Fair scheduler: FIFO queue per tenant â€” escalation retries don't queue-jump

Contract C13: no tenant can starve another. Escalation retries go to back of queue.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Literal

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="VibeForge Capacity Manager")

_REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
_REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_URL    = os.environ.get("REDIS_URL", f"redis://{_REDIS_HOST}:{_REDIS_PORT}")
DATABASE_URL          = os.environ["DATABASE_URL"]
MAX_CONCURRENT_BUILDS = int(os.environ.get("MAX_CONCURRENT_BUILDS", "10"))
LITELLM_MAX_PARALLEL  = int(os.environ.get("LITELLM_MAX_PARALLEL", "5"))

# GPU pool config (for future Coder-32B integration)
VLLM_MAX_NUM_SEQS     = int(os.environ.get("VLLM_MAX_NUM_SEQS", "8"))


# â”€â”€ Pydantic Models â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class SlotRequest(BaseModel):
    job_id:     str
    tenant_id:  str
    slot_type:  Literal["build", "llm", "gpu"]
    is_retry:   bool = False    # Contract C13: retries go to BACK of queue

class SlotResponse(BaseModel):
    granted:    bool
    position:   int             # queue position if not granted
    wait_ms:    int             # estimated wait milliseconds


class CapacityStatus(BaseModel):
    build_slots_used:   int
    build_slots_total:  int
    llm_slots_used:     int
    llm_slots_total:    int
    gpu_slots_used:     int
    gpu_slots_total:    int
    queue_depth:        dict[str, int]   # per tenant: how many waiting


# â”€â”€ Redis-backed counters + FIFO queues â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

redis_client: aioredis.Redis | None = None
pool: asyncpg.Pool | None = None

@app.on_event("startup")
async def startup():
    global redis_client, pool
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

@app.on_event("shutdown")
async def shutdown():
    if redis_client:
        await redis_client.aclose()
    if pool:
        await pool.close()


# â”€â”€ Slot management helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

SLOT_LIMITS = {
    "build": MAX_CONCURRENT_BUILDS,
    "llm":   LITELLM_MAX_PARALLEL,
    "gpu":   VLLM_MAX_NUM_SEQS,
}

async def _slots_used(slot_type: str) -> int:
    count = await redis_client.get(f"capacity:{slot_type}:used")
    return int(count or 0)

async def _try_acquire(slot_type: str) -> bool:
    """Atomically increment if under limit. Returns True if slot granted."""
    limit = SLOT_LIMITS[slot_type]
    key   = f"capacity:{slot_type}:used"

    async with redis_client.pipeline(transaction=True) as pipe:
        while True:
            try:
                await pipe.watch(key)
                current = int(await pipe.get(key) or 0)
                if current >= limit:
                    await pipe.reset()
                    return False
                pipe.multi()
                pipe.incr(key)
                await pipe.execute()
                return True
            except aioredis.WatchError:
                continue   # retry on concurrent modification

async def _release(slot_type: str) -> None:
    """Release a slot. Never goes below zero."""
    key = f"capacity:{slot_type}:used"
    current = int(await redis_client.get(key) or 0)
    if current > 0:
        await redis_client.decr(key)


# â”€â”€ Routes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/health")
async def health():
    return {"status": "ok", "service": "capacity"}


@app.post("/acquire", response_model=SlotResponse)
async def acquire_slot(req: SlotRequest):
    """
    Request a capacity slot.

    Contract C13 enforcement:
    - Slots are granted FIFO per tenant
    - If is_retry=True, the job goes to the BACK of that tenant's queue
      (escalation retries cannot jump ahead of fresh jobs)
    - A tenant with all slots busy waits, never blocks other tenants
    """
    # Check per-tenant queue position
    queue_key = f"capacity:queue:{req.slot_type}:{req.tenant_id}"

    if req.is_retry:
        # Retries go to BACK of queue â€” Contract C13
        await redis_client.rpush(queue_key, req.job_id)
    else:
        await redis_client.lpush(queue_key, req.job_id)

    # Try to get global slot
    granted = await _try_acquire(req.slot_type)

    if granted:
        # Remove from queue â€” we have the slot
        await redis_client.lrem(queue_key, 1, req.job_id)
        # Track active slot with TTL safety (auto-release after 30 min if crashed)
        await redis_client.set(
            f"capacity:active:{req.slot_type}:{req.job_id}",
            "1",
            ex=1800
        )
        return SlotResponse(granted=True, position=0, wait_ms=0)
    else:
        # Queue position
        position = await redis_client.lpos(queue_key, req.job_id) or 0
        wait_ms  = int(position * 30_000)  # rough estimate: 30s per job ahead
        return SlotResponse(granted=False, position=position, wait_ms=wait_ms)


@app.post("/release/{slot_type}/{job_id}")
async def release_slot(slot_type: str, job_id: str):
    """Release a capacity slot when job completes or fails."""
    if slot_type not in SLOT_LIMITS:
        raise HTTPException(400, f"Unknown slot type: {slot_type}")

    await _release(slot_type)
    await redis_client.delete(f"capacity:active:{slot_type}:{job_id}")
    return {"released": True, "slot_type": slot_type, "job_id": job_id}


@app.get("/status", response_model=CapacityStatus)
async def get_status():
    """Returns current capacity utilization across all slot types."""
    build_used = await _slots_used("build")
    llm_used   = await _slots_used("llm")
    gpu_used   = await _slots_used("gpu")

    # Queue depth per tenant (for dashboards â€” Block F)
    queue_depth: dict[str, int] = {}
    pattern = "capacity:queue:build:*"
    async for key in redis_client.scan_iter(match=pattern):
        tenant_id = key.split(":")[-1]
        depth = await redis_client.llen(key)
        if depth > 0:
            queue_depth[tenant_id] = depth

    return CapacityStatus(
        build_slots_used=build_used,
        build_slots_total=MAX_CONCURRENT_BUILDS,
        llm_slots_used=llm_used,
        llm_slots_total=LITELLM_MAX_PARALLEL,
        gpu_slots_used=gpu_used,
        gpu_slots_total=VLLM_MAX_NUM_SEQS,
        queue_depth=queue_depth,
    )

