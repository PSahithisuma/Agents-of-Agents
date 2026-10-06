from __future__ import annotations

import asyncio
import json
import time
import logging
from datetime import datetime, timezone

logger = logging.getLogger("vibeforge.worker.dummy")

# Five-phase dummy pipeline — proves the async event pipeline end-to-end.
# Real agents replace these phases in Phase 1+.
_PHASES = [
    "Initialising scaffold",
    "Synthesising core module",
    "Synthesising integration layer",
    "Running QA gate checks",
    "Assembling delivery bundle",
]


async def run_dummy_job(ctx: dict, *, job_id: str, tenant_id: str) -> dict:
    """
    Phase 0 dummy generation task.

    1. Marks the job 'running' in Postgres.
    2. Sleeps 2 s between each of 5 synthetic phases.
    3. Each phase writes a job_event row to Postgres AND publishes to Redis
       pub/sub so the SSE endpoint gets it instantly.
    4. Marks the job 'completed'.
    5. Records Prometheus metrics (active_jobs, job_duration).

    ctx keys:
      db_pool            — asyncpg.Pool
      redis              — ArqRedis
      active_jobs_gauge  — prometheus_client.Gauge (optional, safe if absent)
      job_duration_hist  — prometheus_client.Histogram (optional)
      jobs_processed_ctr — prometheus_client.Counter (optional)
    """
    pool    = ctx["db_pool"]
    redis   = ctx["redis"]
    t_start = time.monotonic()
    status  = "completed"

    # Track active jobs
    active_gauge = ctx.get("active_jobs_gauge")
    if active_gauge:
        active_gauge.inc()

    logger.info("job_start job_id=%s tenant_id=%s", job_id, tenant_id)

    # ── 1. Mark running ────────────────────────────────────────────────────
    async with pool.acquire() as conn:
        # SET is transaction-local (asyncpg auto-commits DDL outside txn)
        await conn.execute(f"SET app.current_tenant_id = '{tenant_id}'")
        await conn.execute(
            "UPDATE jobs SET status = 'running', started_at = $1 WHERE id = $2",
            datetime.now(timezone.utc),
            job_id,
        )

    # ── 2. Emit 5 progress events ──────────────────────────────────────────
    for step, phase in enumerate(_PHASES, start=1):
        await asyncio.sleep(2)

        payload = {
            "step": step,
            "total": len(_PHASES),
            "pct": step * 20,
            "phase": phase,
            "message": f"[{step}/{len(_PHASES)}] {phase}",
        }

        async with pool.acquire() as conn:
            await conn.execute(f"SET app.current_tenant_id = '{tenant_id}'")
            await conn.execute(
                """
                INSERT INTO job_events (tenant_id, job_id, event_type, payload)
                VALUES ($1, $2, 'progress', $3::jsonb)
                """,
                tenant_id,
                job_id,
                json.dumps(payload),
            )

        # Publish to Redis → SSE subscribers receive this instantly
        await redis.publish(
            f"job:{job_id}",
            json.dumps({"event_type": "progress", "payload": payload}),
        )

        logger.info("job_progress job_id=%s pct=%d phase=%s", job_id, payload["pct"], phase)

    # ── 3. Mark completed ──────────────────────────────────────────────────
    final = {"status": "completed", "message": "Generation complete (Phase 0 dummy)"}

    async with pool.acquire() as conn:
        await conn.execute(f"SET app.current_tenant_id = '{tenant_id}'")
        await conn.execute(
            "UPDATE jobs SET status = 'completed', finished_at = $1 WHERE id = $2",
            datetime.now(timezone.utc),
            job_id,
        )
        await conn.execute(
            """
            INSERT INTO job_events (tenant_id, job_id, event_type, payload)
            VALUES ($1, $2, 'complete', $3::jsonb)
            """,
            tenant_id,
            job_id,
            json.dumps(final),
        )

    await redis.publish(
        f"job:{job_id}",
        json.dumps({"event_type": "complete", "payload": final}),
    )

    logger.info("job_complete job_id=%s", job_id)

    # Record Prometheus metrics
    elapsed = time.monotonic() - t_start
    if active_gauge:
        active_gauge.dec()
    duration_hist = ctx.get("job_duration_hist")
    if duration_hist:
        duration_hist.labels(job_type="generation", status=status).observe(elapsed)
    jobs_ctr = ctx.get("jobs_processed_ctr")
    if jobs_ctr:
        jobs_ctr.labels(job_type="generation", status=status).inc()

    return {"status": status, "job_id": job_id}
