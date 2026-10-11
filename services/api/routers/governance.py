from __future__ import annotations
import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from core.auth import AuthUser, get_current_user
from core.database import get_tenant_session

router = APIRouter(prefix="/api/v1/governance", tags=["governance"])


async def _admin(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    if "admin" not in set(user.roles or []):
        raise HTTPException(403, "requires admin role")
    return user


@router.get("/cost", summary="LLM and escalation cost for the tenant (last 30 days) plus budget state")
async def cost(user: AuthUser = Depends(get_current_user)):
    async with get_tenant_session(user.tenant_id) as s:
        daily = (await s.execute(text("""SELECT created_at::date AS d, count(*) AS calls, coalesce(sum(cost_usd),0) AS usd
                                         FROM llm_calls WHERE created_at > now() - interval '30 days' GROUP BY 1 ORDER BY 1"""))).mappings().all()
        esc = (await s.execute(text("""SELECT count(*) AS n, count(*) FILTER (WHERE avoided_via_memory) AS avoided_via_memory,
                                       count(*) FILTER (WHERE outcome='gate_pass') AS gate_pass, coalesce(sum(cost_usd),0) AS usd
                                       FROM escalation_register"""))).mappings().one()
        bud = (await s.execute(text("SELECT budget_usd, spent_usd, period_end FROM budgets ORDER BY period_end DESC NULLS LAST LIMIT 1"))).mappings().one_or_none()
        cache = (await s.execute(text("SELECT count(*) AS entries, coalesce(sum(hit_count),0) AS hits FROM cache_entries"))).mappings().one()
    return {"daily": [{"date": str(r["d"]), "calls": int(r["calls"]), "usd": float(r["usd"])} for r in daily],
            "escalation": {k: (float(v) if k == "usd" else int(v)) for k, v in esc.items()},
            "budget": ({"budget_usd": float(bud["budget_usd"]), "spent_usd": float(bud["spent_usd"]), "period_end": str(bud["period_end"])} if bud else None),
            "cache": {k: int(v) for k, v in cache.items()}}


@router.get("/escalations", summary="Escalation register and metacognition gate decisions (HDPO training signal)")
async def escalations(user: AuthUser = Depends(get_current_user)):
    async with get_tenant_session(user.tenant_id) as s:
        reg = (await s.execute(text("""SELECT job_id, trigger_reason, avoided_via_memory, commercial_model, prompt_tokens, cost_usd, outcome, pii_scan_clean, created_at
                                       FROM escalation_register ORDER BY created_at DESC LIMIT 50"""))).mappings().all()
        dec = (await s.execute(text("""SELECT decision_point, tier, outcome, count(*) AS n FROM gate_decisions GROUP BY 1,2,3 ORDER BY n DESC"""))).mappings().all()
        mem = (await s.execute(text("SELECT count(*) AS n, count(*) FILTER (WHERE scope <> 'tenant') AS shared FROM escalation_memory WHERE gate_verified"))).mappings().one()
    return {"register": [{**{k: (str(v) if k in ("job_id", "created_at") else (float(v) if k == "cost_usd" else v)) for k, v in r.items()}} for r in reg],
            "gate_decisions": [dict(r) for r in dec], "memory": dict(mem)}


async def _plan(session):
    tabs = (await session.execute(text("""SELECT table_name FROM information_schema.columns
                                          WHERE table_schema='public' AND column_name='tenant_id' ORDER BY 1"""))).scalars().all()
    counts = {}
    for t in tabs:
        counts[t] = int((await session.execute(text(f'SELECT count(*) FROM "{t}"'))).scalar())
    return counts


@router.get("/erasure-plan", summary="Dry run: what a tenant erasure would delete (row counts per table)")
async def erasure_plan(user: AuthUser = Depends(_admin)):
    async with get_tenant_session(user.tenant_id) as s:
        return {"tenant_id": str(user.tenant_id), "rows": await _plan(s)}


@router.post("/erasure-request", summary="Record a tenant erasure request (audited). Execution is a separate operator step: ops/tenant_erasure.sh")
async def erasure_request(user: AuthUser = Depends(_admin)):
    async with get_tenant_session(user.tenant_id) as s:
        plan = await _plan(s)
        await s.execute(text("""INSERT INTO audit_log (tenant_id, action, resource_type, resource_id, diff_data)
                                VALUES (:t,'tenant.erasure_requested','tenant',:t,cast(:d as jsonb))"""),
                        {"t": str(user.tenant_id), "d": json.dumps({"by": str(user.id), "rows": plan})})
    return {"status": "requested", "rows": plan,
            "next": f"operator runs: ops/tenant_erasure.sh {user.tenant_id} --execute (dry run without --execute)"}


from pydantic import BaseModel


class BudgetIn(BaseModel):
    budget_usd: float
    period_days: int = 30


@router.put("/budget", summary="Set the tenant budget for a new period (admin). Generation returns 402 when it is spent.")
async def set_budget(body: BudgetIn, user: AuthUser = Depends(_admin)):
    if body.budget_usd < 0 or not (1 <= body.period_days <= 366):
        raise HTTPException(422, "budget_usd must be >= 0 and period_days 1-366")
    async with get_tenant_session(user.tenant_id) as s:
        await s.execute(text("""INSERT INTO budgets (tenant_id, budget_usd, spent_usd, period_start, period_end)
                                VALUES (:t, :b, 0, current_date, current_date + :d)"""),
                        {"t": str(user.tenant_id), "b": body.budget_usd, "d": body.period_days})
        await s.execute(text("""INSERT INTO audit_log (tenant_id, action, resource_type, resource_id, diff_data)
                                VALUES (:t,'budget.set','tenant',:t,cast(:d as jsonb))"""),
                        {"t": str(user.tenant_id), "d": json.dumps({"by": str(user.id), "budget_usd": body.budget_usd, "period_days": body.period_days})})
    return {"status": "set", "budget_usd": body.budget_usd, "period_days": body.period_days}


def _score(gates):
    def last(kind):
        g = [x for x in gates if x.get("gate") == kind]
        return g[-1] if g else None
    comp, boot, ut, pv = last("compile"), last("boot"), last("unit_tests"), last("provenance")
    first = next((x for x in gates if x.get("gate") == "compile"), None)
    parts = {"compile": bool(comp and comp.get("status") == "ok"), "boot": bool(boot and boot.get("status") == "ok"),
             "unit_tests": bool(ut and ut.get("status") == "ok"), "provenance_clean": pv is None or pv.get("status") == "ok",
             "compile_first_try": bool(first and first.get("status") == "ok")}
    weights = {"compile": 30, "boot": 30, "unit_tests": 20, "provenance_clean": 10, "compile_first_try": 10}
    return sum(weights[k] for k, ok in parts.items() if ok), parts


@router.get("/eval", summary="Quality score per recent generation job (compile 30, boot 30, unit tests 20, provenance 10, first-try 10)")
async def eval_scores(limit: int = 20, user: AuthUser = Depends(get_current_user)):
    limit = max(1, min(limit, 100))
    async with get_tenant_session(user.tenant_id) as s:
        jobs = (await s.execute(text("""SELECT j.id::text AS id, j.status, j.created_at,
                                        EXTRACT(EPOCH FROM (j.finished_at - j.started_at)) AS secs,
                                        sp.spec_data->>'app_name' AS app, sp.spec_data->>'vertical' AS vertical
                                        FROM jobs j LEFT JOIN application_specs sp ON sp.id = j.spec_id
                                        WHERE j.job_type = 'generation' ORDER BY j.created_at DESC LIMIT :n"""), {"n": limit})).mappings().all()
        ids = [j["id"] for j in jobs]
        gates = (await s.execute(text("""SELECT job_id::text AS jid, iteration, report_data FROM gate_reports
                                         WHERE job_id::text = ANY(CAST(:ids AS text[])) ORDER BY iteration"""), {"ids": ids})).mappings().all()
        esc = (await s.execute(text("""SELECT job_id::text AS jid, count(*) AS n FROM escalation_register
                                       WHERE job_id::text = ANY(CAST(:ids AS text[])) GROUP BY 1"""), {"ids": ids})).mappings().all()
    by_job = {}
    for g in gates:
        rd = g["report_data"]
        by_job.setdefault(g["jid"], []).append(json.loads(rd) if isinstance(rd, str) else rd)
    esc_n = {e["jid"]: int(e["n"]) for e in esc}
    out = []
    for j in jobs:
        gl = by_job.get(j["id"], [])
        score, parts = _score(gl)
        out.append({"job_id": j["id"], "app": j["app"], "vertical": j["vertical"], "status": j["status"],
                    "seconds": round(float(j["secs"]), 1) if j["secs"] is not None else None,
                    "compile_rounds": sum(1 for x in gl if x.get("gate") == "compile"), "escalations": esc_n.get(j["id"], 0),
                    "score": score if gl else None, "checks": parts if gl else None})
    scored = [o["score"] for o in out if o["score"] is not None]
    return {"jobs": out, "summary": {"jobs": len(out), "scored": len(scored),
                                     "mean_score": round(sum(scored) / len(scored), 1) if scored else None,
                                     "perfect": sum(1 for x in scored if x == 100)}}
