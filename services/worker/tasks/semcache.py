import os, uuid, json, logging
from tasks.escalation import _tx

logger = logging.getLogger(__name__)
STACK = "java_spring"
SCAFFOLD_VERSION = os.environ.get("SCAFFOLD_VERSION", "1.0.0")
RULESET_VERSION = os.environ.get("RULESET_VERSION", "1.0.0")


async def write(tenant_id, job_id, spec_data, result):
    if not isinstance(spec_data, dict) or not isinstance(result, dict) or result.get("status") == "failed":
        return
    if result.get("skipped"):
        return
    h = spec_data.get("canonical_hash")
    if not h:
        return
    names = [e.get("name") for e in (spec_data.get("domain_model") or {}).get("entities") or [] if isinstance(e, dict)]
    summary = str(spec_data.get("description") or spec_data.get("app_name") or "")[:200]
    vertical = str(spec_data.get("vertical") or spec_data.get("pack") or "")
    await _tx(tenant_id, "INSERT INTO cache_entries (tenant_id, canonical_hash, stack_profile, scaffold_version, ruleset_version, job_id, vertical, "
              "entity_names, entity_count, summary) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10) "
              "ON CONFLICT (tenant_id, canonical_hash, stack_profile, scaffold_version, ruleset_version) "
              "DO UPDATE SET job_id=EXCLUDED.job_id, entity_names=EXCLUDED.entity_names, summary=EXCLUDED.summary, created_at=now()",
              uuid.UUID(tenant_id), h, STACK, SCAFFOLD_VERSION, RULESET_VERSION, uuid.UUID(job_id), vertical,
              json.dumps(names), len(names), summary)
    logger.info(f"semantic cache written for job {job_id} hash {h[:12]}")
