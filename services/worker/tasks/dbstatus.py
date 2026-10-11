import os, json, logging, uuid
import asyncpg

logger = logging.getLogger(__name__)

def _dsn():
    u = os.environ.get("DATABASE_URL", "")
    for prefix in ("postgresql+asyncpg://", "postgres+asyncpg://", "postgres://"):
        if u.startswith(prefix):
            return "postgresql://" + u[len(prefix):]
    return u

async def _run(tenant_id, sql, *args):
    try:
        conn = await asyncpg.connect(_dsn(), timeout=10)
        try:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_id))
                await conn.execute(sql, *args)
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("db status write failed: %s", str(e)[:200])

async def db_mark_running(tenant_id, job_id):
    await _run(tenant_id, "UPDATE jobs SET status='running', started_at=now() WHERE id=$1", uuid.UUID(str(job_id)))

async def db_mark_done(tenant_id, job_id, status, result_ref, error=None):
    await _run(tenant_id, "UPDATE jobs SET status=$2, finished_at=now(), result_ref=$3, error_detail=$4::jsonb WHERE id=$1",
               uuid.UUID(str(job_id)), status, result_ref, json.dumps(error) if error else None)

async def db_add_event(tenant_id, job_id, event_type, payload):
    await _run(tenant_id, "INSERT INTO job_events (tenant_id, job_id, event_type, payload) VALUES ($1, $2, $3, $4::jsonb)",
               uuid.UUID(str(tenant_id)), uuid.UUID(str(job_id)), event_type, json.dumps(payload))


async def db_audit(tenant_id, action, resource_type, resource_id, diff=None):
    # Append-only audit trail (RLS allows INSERT/SELECT only).
    await _run(tenant_id,
               "INSERT INTO audit_log (tenant_id, action, resource_type, resource_id, diff_data) VALUES ($1, $2, $3, $4, $5::jsonb)",
               uuid.UUID(str(tenant_id)), action, resource_type, uuid.UUID(str(resource_id)),
               json.dumps(diff if diff is not None else {}, default=str))


async def db_gate_report(tenant_id, job_id, iteration, passed, report):
    # INSERT only (RLS has no UPDATE policy on gate_reports).
    await _run(tenant_id,
               "INSERT INTO gate_reports (tenant_id, job_id, iteration, passed, report_data) VALUES ($1, $2, $3, $4, $5::jsonb) ON CONFLICT (job_id, iteration) DO NOTHING",
               uuid.UUID(str(tenant_id)), uuid.UUID(str(job_id)), int(iteration), bool(passed),
               json.dumps(report, default=lambda o: sorted(o) if isinstance(o, (set, frozenset)) else str(o)))
