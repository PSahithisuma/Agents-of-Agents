"""
VibeForge — Arq Worker (Phase 1 wired)
=======================================
Arq background worker. Picks up jobs from Redis and runs them.

Tasks registered:
    run_generation   — full generation pipeline (real agents)
    validate_spec    — completeness check + gap questions only
    dummy_job        — kept for smoke testing

Start:
    python worker.py
"""

from __future__ import annotations

import logging
import os

from arq import cron
from arq.connections import RedisSettings

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
)

# ── Redis settings ─────────────────────────────────────────────────────────────
REDIS_HOST     = os.getenv("REDIS_HOST", "redis")
REDIS_PORT     = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None   # Memorystore: no auth (None = no AUTH sent)
REDIS_DB       = int(os.getenv("REDIS_DB", "0"))


# ── Startup / shutdown hooks ───────────────────────────────────────────────────

async def startup(ctx):
    logger.info("[Worker] Starting up — Redis %s:%d", REDIS_HOST, REDIS_PORT)
    # A restart kills any in-flight job and Arq does not requeue it, so rows still marked
    # 'running' are orphans. Mark them failed so they stop blocking resubmits (spec cache lookup).
    try:
        import asyncpg
        _url = os.getenv("DATABASE_URL")
        if _url:
            _conn = await asyncpg.connect(_url.replace("+asyncpg", ""))
            try:
                _res = await _conn.execute(
                    "UPDATE jobs SET status='failed' WHERE status='running'"
                )
                logger.warning("[Worker] orphaned running jobs on startup: %s", _res)
            finally:
                await _conn.close()
    except Exception as e:
        logger.error("[Worker] orphan cleanup skipped: %s", e)


async def shutdown(ctx):
    logger.info("[Worker] Shutting down")


# ── Task imports ───────────────────────────────────────────────────────────────

async def dummy_job(ctx, job_id: str, **kwargs) -> dict:
    """Smoke-test task — kept for CI and health checks."""
    import asyncio
    logger.info("[Worker] dummy_job %s", job_id)

    steps = [
        ("planning",    "started",  {"message": "Planning modules"}),
        ("planning",    "complete", {"message": "Plan ready", "module_count": 3}),
        ("synthesizing","started",  {"message": "Synthesizing code"}),
        ("synthesizing","complete", {"message": "Code generated", "files": 9}),
        ("gate",        "passed",   {"message": "QA gate passed"}),
        ("delivery",    "complete", {"message": "Done"}),
    ]

    try:
        import asyncpg
        DATABASE_URL = os.getenv(
            "DATABASE_URL",
            "postgresql://vibeforge:vibeforge_dev_secret@postgres:5432/vibeforge"
        )
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            for phase, status, data in steps:
                await asyncio.sleep(0.5)
                import json
                await conn.execute(
                    "INSERT INTO job_events (job_id, phase, status, data, created_at) "
                    "VALUES ($1, $2, $3, $4, NOW())",
                    job_id, phase, status, json.dumps(data),
                )
            await conn.execute(
                "UPDATE jobs SET status='completed', finished_at=NOW(), updated_at=NOW() WHERE id=$1",
                job_id,
            )
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("[Worker] dummy_job DB write failed: %s — continuing", e)

    return {"status": "completed", "job_id": job_id}


# ── Real generation tasks ──────────────────────────────────────────────────────

async def run_generation(ctx, job_id: str, tenant_id: str, spec_data: dict) -> dict:
    """Full generation pipeline — calls real agents."""
    from tasks.generation import run_generation as _run
    return await _run(ctx, job_id=job_id, tenant_id=tenant_id, spec_data=spec_data)


async def validate_spec(ctx, job_id: str, tenant_id: str, spec_data: dict) -> dict:
    """Completeness validation + gap questions — no generation."""
    from tasks.generation import validate_spec as _validate
    return await _validate(ctx, job_id=job_id, tenant_id=tenant_id, spec_data=spec_data)


# ── Arq worker settings ────────────────────────────────────────────────────────

class WorkerSettings:
    functions = [
        dummy_job,
        run_generation,
        validate_spec,
    ]

    redis_settings = RedisSettings(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        database=REDIS_DB,
    )

    on_startup  = startup
    on_shutdown = shutdown

    max_jobs          = 5
    job_timeout       = 3600   # 1 hour max per job
    keep_result       = 3600   # keep results for 1 hour
    poll_delay        = 0.5    # check Redis every 500ms
    retry_jobs        = False  # don't auto-retry — generation is expensive


if __name__ == '__main__':
    import asyncio
    from arq import run_worker
    asyncio.run(run_worker(WorkerSettings))
