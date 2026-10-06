from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse
from sqlalchemy import text

from core.auth import AuthUser, get_current_user
from core.database import get_tenant_session
from core.redis_client import get_redis

router = APIRouter(prefix="/api/v1/jobs", tags=["stream"])

_TERMINAL: frozenset[str] = frozenset({"completed", "failed", "cancelled"})


@router.get(
    "/{job_id}/stream",
    response_class=EventSourceResponse,
    summary="Stream job progress events via SSE",
)
async def stream_job_events(
    job_id: UUID,
    user: AuthUser = Depends(get_current_user),
) -> EventSourceResponse:
    """
    Server-Sent Events endpoint — streams job_events in real-time.

    Flow:
    • If job is still running → subscribe to Redis pub/sub channel `job:{job_id}`.
      Each progress event emitted by the Arq worker is forwarded instantly.
      Stream closes when we receive an event with event_type == 'complete' | 'error'.

    • If job is already in a terminal state → replay all historical events from
      Postgres and immediately close — the client gets the full picture without
      waiting on pub/sub.

    Auth: tenant_id from JWT → RLS enforced on DB queries.
    """
    # Verify the job exists and belongs to this tenant
    async with get_tenant_session(user.tenant_id) as session:
        row = (
            await session.execute(
                text("SELECT status FROM jobs WHERE id = :id"),
                {"id": str(job_id)},
            )
        ).one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")

    current_status: str = row.status

    # ── Generator ─────────────────────────────────────────────────────────────

    async def event_generator():

        # ── Path A: job already terminal — replay from DB ──────────────────
        if current_status in _TERMINAL:
            async with get_tenant_session(user.tenant_id) as session:
                events = (
                    await session.execute(
                        text("""
                            SELECT event_type, payload
                            FROM job_events
                            WHERE job_id = :job_id
                            ORDER BY seq ASC
                        """),
                        {"job_id": str(job_id)},
                    )
                ).mappings().all()

            for evt in events:
                yield {
                    "event": evt["event_type"],
                    "data": json.dumps({
                        "event_type": evt["event_type"],
                        "payload": evt["payload"],
                    }),
                }

            # Synthetic terminal signal so client can close cleanly
            yield {
                "event": "done",
                "data": json.dumps({"status": current_status}),
            }
            return

        # ── Path B: job running — subscribe to Redis pub/sub ───────────────
        redis = get_redis()
        pubsub = redis.pubsub()
        channel = f"job:{job_id}"
        await pubsub.subscribe(channel)

        # Emit immediate heartbeat so frontend knows the stream is connected
        yield {
            "event": "heartbeat",
            "data": json.dumps({
                "event_type": "heartbeat",
                "payload": {
                    "progress": 5,
                    "message": "Job queued in worker engine...",
                    "status": current_status,
                },
            }),
        }

        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    # 'subscribe' ack messages — skip
                    continue

                raw: str = message["data"]
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                event_type: str = parsed.get("event_type") or parsed.get("stage", "message")

                yield {"event": event_type, "data": raw}

                # Close the generator on terminal event — client will disconnect
                if event_type in ("complete", "error"):
                    yield {
                        "event": "done",
                        "data": json.dumps({"status": event_type}),
                    }
                    break

        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    return EventSourceResponse(event_generator())
