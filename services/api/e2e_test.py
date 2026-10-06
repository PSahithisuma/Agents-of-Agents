"""
Combined Phase 0 + Phase 1 + Phase 1 Extension end-to-end test for VibeForge.

Phase 0 checks (7):
  [P0-1]  GET /health                  → postgres + redis ok
  [P0-2]  POST /projects/              → 201 with project_id + tenant_id
  [P0-3]  POST /projects/{id}/generate → 202 with job_id + stream_url
  [P0-4]  Worker picks up job          → status queued→running→completed
  [P0-5]  GET /jobs/{id}               → status=completed
  [P0-6]  GET /jobs/{id}/events        → 6 rows in Postgres
  [P0-7]  Idempotency                  → second generate returns same job_id (cache_hit)

Phase 1 checks (7):
  [P1-1]  GET /option-graph            → 8 option keys returned
  [P1-2]  POST /specs/                 → AppSpec compiled, canonical_hash returned
  [P1-3]  Determinism                  → same selections → same canonical_hash
  [P1-4]  POST /generate (cache miss)  → new job, cache_hit=False
  [P1-5]  POST /generate (cache hit)   → same job_id, cache_hit=True (P3)
  [P1-6]  GET /metrics                 → Prometheus live with vibeforge counters
  [P1-7]  Worker + events              → 6 events persisted in Postgres

Phase 1 Extension checks (4):
  [P1-8]  GET  retrieval/health        → retrieval service up
  [P1-9]  POST /retrieve               → returns list (may be empty; no error)
  [P1-10] GET  litellm/health          → LiteLLM proxy up + agent-model listed
  [P1-11] GET  ui health               → Streamlit responding on port 8501
"""
import asyncio
import sys

import httpx

BASE = "http://localhost:8000"
KC   = "http://keycloak:8080"

# ANSI colours
GREEN  = "\033[92m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

results: list[tuple[str, bool, str]] = []   # (label, passed, detail)


def ok(label: str, detail: str = "") -> None:
    results.append((label, True, detail))
    print(f"  {GREEN}✓{RESET} {label}" + (f"  {detail}" if detail else ""))


def fail(label: str, detail: str = "") -> None:
    results.append((label, False, detail))
    print(f"  {RED}✗{RESET} {label}" + (f"  {detail}" if detail else ""))


# =============================================================================
async def main() -> None:
    async with httpx.AsyncClient(timeout=30.0) as c:

        # ── Authenticate ─────────────────────────────────────────────────────
        print(f"\n{BOLD}{CYAN}── Auth ─────────────────────────────────────────{RESET}")
        body = (
            "client_id=vibeforge-api"
            "&client_secret=vibeforge-api-secret-change-me"
            "&username=admin@vibeforge.local"
            "&password=admin123"
            "&grant_type=password"
        )
        r = await c.post(
            f"{KC}/realms/vibeforge/protocol/openid-connect/token",
            content=body.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        token = r.json()["access_token"]
        hdrs  = {"Authorization": f"Bearer {token}"}
        print(f"  Token OK  length={len(token)}")

        # =====================================================================
        print(f"\n{BOLD}{CYAN}── Phase 0: Async Pipeline ──────────────────────{RESET}")
        # =====================================================================

        # [P0-1] Health check
        r = await c.get(f"{BASE}/health")
        health = r.json()
        if r.status_code == 200 and health.get("status") == "healthy":
            ok("[P0-1] GET /health", f"postgres={health['checks']['postgres']}  redis={health['checks']['redis']}")
        else:
            fail("[P0-1] GET /health", r.text)

        # [P0-2] Create project
        r = await c.post(
            f"{BASE}/api/v1/projects/",
            json={"name": "Phase-0 Test App"},
            headers=hdrs,
        )
        if r.status_code == 201:
            p0_proj = r.json()
            ok("[P0-2] POST /projects/", f"id={p0_proj['id']}")
        else:
            fail("[P0-2] POST /projects/", r.text)
            p0_proj = None

        # [P0-3] Enqueue job (no selections = all defaults)
        if p0_proj:
            r = await c.post(
                f"{BASE}/api/v1/projects/{p0_proj['id']}/generate",
                json={},
                headers=hdrs,
            )
            if r.status_code == 202:
                p0_gen = r.json()
                ok("[P0-3] POST /generate", f"job_id={p0_gen['job_id']}")
            else:
                fail("[P0-3] POST /generate", r.text)
                p0_gen = None
        else:
            fail("[P0-3] POST /generate", "skipped — no project")
            p0_gen = None

        # [P0-4] Poll until completed
        if p0_gen:
            p0_job_id = p0_gen["job_id"]
            p0_status = "queued"
            for attempt in range(15):
                await asyncio.sleep(3)
                r = await c.get(f"{BASE}/api/v1/jobs/{p0_job_id}", headers=hdrs)
                p0_status = r.json().get("status", "unknown")
                if p0_status in ("completed", "failed"):
                    break
            if p0_status == "completed":
                ok("[P0-4] Worker completed job", f"status={p0_status}")
            else:
                fail("[P0-4] Worker completed job", f"status={p0_status} after 45s")
        else:
            fail("[P0-4] Worker completed job", "skipped")

        # [P0-5] GET /jobs/{id}
        if p0_gen:
            r = await c.get(f"{BASE}/api/v1/jobs/{p0_job_id}", headers=hdrs)
            job = r.json()
            if job.get("status") == "completed":
                ok("[P0-5] GET /jobs/{id}", "status=completed")
            else:
                fail("[P0-5] GET /jobs/{id}", f"status={job.get('status')}")

        # [P0-6] Events in Postgres
        if p0_gen:
            r = await c.get(f"{BASE}/api/v1/jobs/{p0_job_id}/events", headers=hdrs)
            events = r.json()
            if len(events) == 6:
                ok("[P0-6] 6 job_events in Postgres", f"seq {events[0]['seq']}–{events[-1]['seq']}")
            else:
                fail("[P0-6] 6 job_events in Postgres", f"got {len(events)} events")

        # [P0-7] Idempotency — second generate on same project returns cache_hit
        if p0_gen and p0_proj:
            r = await c.post(
                f"{BASE}/api/v1/projects/{p0_proj['id']}/generate",
                json={},
                headers=hdrs,
            )
            gen2 = r.json()
            if gen2.get("cache_hit") is True and str(gen2.get("job_id")) == str(p0_job_id):
                ok("[P0-7] Idempotency / cache hit", f"same job_id returned")
            else:
                fail("[P0-7] Idempotency / cache hit", f"cache_hit={gen2.get('cache_hit')}  job_id matches={str(gen2.get('job_id')) == str(p0_job_id)}")

        # =====================================================================
        print(f"\n{BOLD}{CYAN}── Phase 1: Spec IR + Option Graph + Metrics ────{RESET}")
        # =====================================================================

        # [P1-1] Option graph
        r = await c.get(f"{BASE}/api/v1/projects/option-graph")
        og = r.json()
        if len(og) == 8:
            ok("[P1-1] GET /option-graph", f"8 keys: {list(og.keys())}")
        else:
            fail("[P1-1] GET /option-graph", f"got {len(og)} keys, expected 8")

        # [P1-2] Compile spec
        r = await c.post(
            f"{BASE}/api/v1/projects/",
            json={"name": "Phase-1 Test App"},
            headers=hdrs,
        )
        p1_proj = r.json()

        SELECTIONS = {
            "auth_strategy":  "jwt",
            "db_tier":        "postgres",
            "ui_framework":   "react",
            "api_style":      "rest",
            "deploy_target":  "docker",
            "async_jobs":     "yes",
            "compliance_tier": "gdpr",
            "search_enabled": "no",
        }
        r = await c.post(
            f"{BASE}/api/v1/projects/{p1_proj['id']}/specs/",
            json={"selections": SELECTIONS, "app_name": "MyApp", "description": "e2e test"},
            headers=hdrs,
        )
        if r.status_code == 201:
            spec1 = r.json()
            canon = spec1["canonical_hash"]
            ok("[P1-2] POST /specs/", f"version={spec1['version']}  hash={canon[:16]}…")
        else:
            fail("[P1-2] POST /specs/", r.text)
            canon = None

        # [P1-3] Determinism — same selections → same hash
        if canon:
            r = await c.post(
                f"{BASE}/api/v1/projects/{p1_proj['id']}/specs/",
                json={"selections": SELECTIONS, "app_name": "MyApp", "description": "e2e test"},
                headers=hdrs,
            )
            spec2 = r.json()
            if spec2["canonical_hash"] == canon:
                ok("[P1-3] canonical_hash is deterministic", f"hash={canon[:16]}… identical")
            else:
                fail("[P1-3] canonical_hash is deterministic", f"{canon[:16]} ≠ {spec2['canonical_hash'][:16]}")

        # [P1-4] Generate — cache miss
        r = await c.post(
            f"{BASE}/api/v1/projects/{p1_proj['id']}/generate",
            json={"selections": SELECTIONS, "app_name": "MyApp", "description": "e2e test"},
            headers=hdrs,
        )
        if r.status_code == 202:
            p1_gen = r.json()
            p1_job_id = p1_gen["job_id"]
            if p1_gen["cache_hit"] is False and p1_gen["canonical_hash"] == canon:
                ok("[P1-4] POST /generate (cache miss)", f"job_id={p1_job_id}  hash={p1_gen['canonical_hash'][:16]}…")
            else:
                fail("[P1-4] POST /generate (cache miss)", f"cache_hit={p1_gen['cache_hit']}")
        else:
            fail("[P1-4] POST /generate (cache miss)", r.text)
            p1_gen = None

        # [P1-5] Generate again — cache hit (P3)
        if p1_gen:
            r = await c.post(
                f"{BASE}/api/v1/projects/{p1_proj['id']}/generate",
                json={"selections": SELECTIONS, "app_name": "MyApp", "description": "e2e test"},
                headers=hdrs,
            )
            p1_gen2 = r.json()
            if p1_gen2["cache_hit"] is True and str(p1_gen2["job_id"]) == str(p1_job_id):
                ok("[P1-5] POST /generate (P3 cache hit)", f"same job_id={p1_job_id}")
            else:
                fail("[P1-5] POST /generate (P3 cache hit)", f"cache_hit={p1_gen2.get('cache_hit')}  job match={str(p1_gen2.get('job_id')) == str(p1_job_id)}")

        # [P1-6] Prometheus /metrics
        metrics_r = await c.get(f"{BASE}/metrics")
        metrics_text = metrics_r.text
        if (metrics_r.status_code == 200
                and "vibeforge_jobs_enqueued_total" in metrics_text
                and "vibeforge_specs_compiled_total" in metrics_text):
            ok("[P1-6] GET /metrics live", f"{len(metrics_text):,} bytes  vibeforge counters present")
        else:
            fail("[P1-6] GET /metrics live", f"status={metrics_r.status_code}")

        # [P1-7] Poll Phase 1 job + verify events
        if p1_gen:
            p1_status = "queued"
            for attempt in range(15):
                await asyncio.sleep(3)
                r = await c.get(f"{BASE}/api/v1/jobs/{p1_job_id}", headers=hdrs)
                p1_status = r.json().get("status", "unknown")
                if p1_status in ("completed", "failed"):
                    break

            r = await c.get(f"{BASE}/api/v1/jobs/{p1_job_id}/events", headers=hdrs)
            p1_events = r.json()
            if p1_status == "completed" and len(p1_events) == 6:
                ok("[P1-7] Worker + 6 events in Postgres", f"seq {p1_events[0]['seq']}–{p1_events[-1]['seq']}")
            else:
                fail("[P1-7] Worker + 6 events in Postgres", f"status={p1_status}  events={len(p1_events)}")

        # =====================================================================
        # Phase 1 Extension — new services
        # Test runs inside the api container → use Docker service names, not localhost
        # =====================================================================
        RETRIEVAL_BASE = "http://retrieval:8001"
        LITELLM_BASE   = "http://litellm:4000"
        UI_BASE        = "http://ui:8501"

        print(f"\n{CYAN}{BOLD}Phase 1 Extension — RAG + LiteLLM + UI{RESET}")

        # [P1-8] Retrieval service health
        try:
            r = await c.get(f"{RETRIEVAL_BASE}/health", timeout=10.0)
            if r.status_code == 200 and r.json().get("status") == "ok":
                ok("[P1-8] Retrieval service /health", "status=ok")
            else:
                fail("[P1-8] Retrieval service /health", f"status={r.status_code} body={r.text[:60]}")
        except Exception as exc:
            fail("[P1-8] Retrieval service /health", f"connection error: {exc}")

        # [P1-9] POST /retrieve — tenant-scoped query
        # Collection may be empty (no docs uploaded yet); any non-500 response is a pass.
        try:
            r = await c.post(
                f"{RETRIEVAL_BASE}/retrieve",
                json={
                    "query":     "what is the authentication strategy",
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                    "top_k":     5,
                },
                timeout=30.0,
            )
            if r.status_code == 200:
                chunks = r.json().get("chunks", [])
                ok("[P1-9] POST /retrieve (C8 tenant-scoped)", f"chunks={len(chunks)} (empty=ok, no docs yet)")
            else:
                fail("[P1-9] POST /retrieve", f"status={r.status_code} body={r.text[:80]}")
        except Exception as exc:
            fail("[P1-9] POST /retrieve", f"connection error: {exc}")

        # [P1-10] LiteLLM proxy health + agent-model listed
        # LiteLLM /health requires master key auth
        LITELLM_KEY = "Bearer sk-vibeforge-litellm-dev-change-me"
        try:
            r = await c.get(
                f"{LITELLM_BASE}/health",
                headers={"Authorization": LITELLM_KEY},
                timeout=15.0,
            )
            if r.status_code == 200:
                r2 = await c.get(
                    f"{LITELLM_BASE}/v1/models",
                    headers={"Authorization": LITELLM_KEY},
                    timeout=10.0,
                )
                if r2.status_code == 200:
                    model_ids = [m["id"] for m in r2.json().get("data", [])]
                    if "agent-model" in model_ids:
                        ok("[P1-10] LiteLLM proxy /health + agent-model", f"models={model_ids}")
                    else:
                        fail("[P1-10] LiteLLM proxy agent-model not listed", f"got: {model_ids}")
                else:
                    fail("[P1-10] LiteLLM /v1/models", f"status={r2.status_code}")
            else:
                fail("[P1-10] LiteLLM proxy /health", f"status={r.status_code} body={r.text[:80]}")
        except Exception as exc:
            fail("[P1-10] LiteLLM proxy /health", f"connection error: {exc}")

        # [P1-11] Streamlit UI health
        try:
            r = await c.get(f"{UI_BASE}/_stcore/health", timeout=10.0)
            if r.status_code == 200:
                ok("[P1-11] Streamlit UI /_stcore/health", "status=ok")
            else:
                fail("[P1-11] Streamlit UI", f"status={r.status_code}")
        except Exception as exc:
            fail("[P1-11] Streamlit UI", f"connection error: {exc}")

        # =====================================================================
        # Summary
        # =====================================================================
        total   = len(results)
        passed  = sum(1 for _, p, _ in results if p)
        failed  = total - passed
        p0_pass = sum(1 for l, p, _ in results if l.startswith("[P0") and p)
        p1_pass = sum(1 for l, p, _ in results if l.startswith("[P1") and p)
        p1x_pass = sum(1 for l, p, _ in results if l.startswith("[P1-8") or l.startswith("[P1-9") or l.startswith("[P1-10") or l.startswith("[P1-11") and p)

        print(f"\n{BOLD}{'─'*56}{RESET}")
        print(f"{BOLD}  Phase 0 (core infra):   {p0_pass}/7  passed{RESET}")
        print(f"{BOLD}  Phase 1 (spec + cache): {p1_pass - p1x_pass}/7  passed{RESET}")
        print(f"{BOLD}  Phase 1 Ext (RAG+UI):   {p1x_pass}/4  passed{RESET}")
        print(f"{BOLD}  TOTAL:                  {passed}/{total} passed{RESET}")
        print(f"{'─'*56}")

        if failed == 0:
            print(f"\n{BOLD}{GREEN}=== ALL {total} CHECKS PASSED ✓ ==={RESET}\n")
        else:
            print(f"\n{RED}=== {failed}/{total} CHECKS FAILED — see above ==={RESET}\n")
            for label, passed, detail in results:
                if not passed:
                    print(f"  {RED}FAILED:{RESET} {label}  {detail}")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
