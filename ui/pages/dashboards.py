"""
Cost Dashboards + Job Console — Phase 3 UI pages
Added to existing Streamlit UI (ui/app.py)

Pages:
  📊 Cost Dashboard  — per-tenant LLM spend, escalation register,
                       gate pass rate, cache hit rate, GPU utilization
  🖥 Job Console     — live node/phase/iteration progress, gate summaries,
                       preview link
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import httpx
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd

API_BASE     = os.environ.get("API_BASE_URL", "http://localhost:8000")
CAPACITY_BASE = os.environ.get("CAPACITY_URL", "http://localhost:8004")
SANDBOX_BASE  = os.environ.get("SANDBOX_URL", "http://localhost:8002")


# =============================================================================
# Page: Cost Dashboard
# =============================================================================

def render_cost_dashboard(token: str):
    st.title("📊 Cost Dashboard")
    st.caption("Per-tenant · Real-time from Cloud Monitoring")

    hdrs = {"Authorization": f"Bearer {token}"}

    # ── Fetch metrics from API ────────────────────────────────────────────────
    try:
        r = httpx.get(f"{API_BASE}/api/v1/metrics/summary", headers=hdrs, timeout=10)
        metrics = r.json() if r.status_code == 200 else {}
    except Exception:
        metrics = {}

    try:
        cap = httpx.get(f"{CAPACITY_BASE}/status", headers=hdrs, timeout=5)
        capacity = cap.json() if cap.status_code == 200 else {}
    except Exception:
        capacity = {}

    # ── KPI Cards ─────────────────────────────────────────────────────────────
    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        llm_spend = metrics.get("llm_spend_usd_30d", 0.0)
        st.metric("LLM Spend (30d)", f"${llm_spend:.4f}",
                  delta=f"${metrics.get('llm_spend_delta', 0):.4f}",
                  help="Total spend via LiteLLM proxy (Vertex AI / Gemini Flash)")

    with col2:
        gate_pass_rate = metrics.get("gate_pass_rate_pct", 0.0)
        st.metric("Gate Pass Rate", f"{gate_pass_rate:.1f}%",
                  delta=f"{metrics.get('gate_pass_rate_delta', 0):.1f}%",
                  delta_color="normal",
                  help="% of QA Gate runs that passed (all 7 steps)")

    with col3:
        cache_hit = metrics.get("cache_hit_rate_pct", 0.0)
        st.metric("Cache Hit Rate", f"{cache_hit:.1f}%",
                  help="Spec hash cache hits — same spec reuses existing job")

    with col4:
        escalations = metrics.get("escalations_total", 0)
        avoided     = metrics.get("escalations_avoided_via_memory", 0)
        st.metric("Escalations", f"{escalations}",
                  delta=f"−{avoided} avoided via memory",
                  delta_color="inverse",
                  help="Times local model failed and commercial API was called")

    with col5:
        gpu_used  = capacity.get("gpu_slots_used", 0)
        gpu_total = capacity.get("gpu_slots_total", 8)
        gpu_pct   = (gpu_used / max(gpu_total, 1)) * 100
        st.metric("GPU Utilization", f"{gpu_pct:.0f}%",
                  help=f"{gpu_used}/{gpu_total} GPU slots in use (vLLM max_num_seqs)")

    st.divider()

    # ── LLM Spend Over Time (line chart) ──────────────────────────────────────
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("LLM Spend — Last 30 Days")
        spend_data = metrics.get("spend_timeseries", [])
        if spend_data:
            df = pd.DataFrame(spend_data)
            fig = px.area(df, x="date", y="spend_usd",
                          color_discrete_sequence=["#6366f1"],
                          labels={"spend_usd": "USD", "date": ""})
            fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), height=200)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No spend data yet — generate your first app to see costs here.")

    with col_right:
        st.subheader("Gate Pass Rate — By Stack Profile")
        gate_by_stack = metrics.get("gate_pass_by_stack", {})
        if gate_by_stack:
            df = pd.DataFrame([
                {"stack": k, "pass_rate": v}
                for k, v in gate_by_stack.items()
            ])
            fig = px.bar(df, x="stack", y="pass_rate", range_y=[0, 100],
                         color="pass_rate",
                         color_continuous_scale=["#ef4444", "#f59e0b", "#22c55e"],
                         labels={"pass_rate": "Pass Rate %", "stack": ""},
                         text_auto=True)
            fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), height=200,
                              coloraxis_showscale=False)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No gate runs yet.")

    st.divider()

    # ── Escalation Register ───────────────────────────────────────────────────
    st.subheader("🚨 Escalation Register")
    st.caption("Every time the local model failed and a commercial API was called")

    escalation_data = metrics.get("escalation_register", [])
    if escalation_data:
        df = pd.DataFrame(escalation_data)
        # Key column: "avoided_via_memory" — shows cases where RAG/cache prevented escalation
        df["avoided_via_memory"] = df["avoided_via_memory"].apply(
            lambda x: "✅ Yes" if x else "❌ No"
        )
        st.dataframe(
            df[["timestamp", "tenant_id", "job_id", "reason",
                "model_called", "cost_usd", "avoided_via_memory"]],
            use_container_width=True,
            hide_index=True,
        )
        avoided_count = sum(1 for e in escalation_data if e.get("avoided_via_memory"))
        st.success(f"**{avoided_count}** escalations avoided via memory (RAG + semantic cache)")
    else:
        st.info("No escalations yet — local model handling all requests.")

    # ── Capacity Utilization ──────────────────────────────────────────────────
    st.divider()
    st.subheader("⚙ Capacity Utilization")

    c1, c2, c3 = st.columns(3)
    with c1:
        build_used  = capacity.get("build_slots_used", 0)
        build_total = capacity.get("build_slots_total", 10)
        st.progress(build_used / max(build_total, 1), text=f"Build: {build_used}/{build_total}")

    with c2:
        llm_used  = capacity.get("llm_slots_used", 0)
        llm_total = capacity.get("llm_slots_total", 5)
        st.progress(llm_used / max(llm_total, 1), text=f"LLM: {llm_used}/{llm_total}")

    with c3:
        gpu_used  = capacity.get("gpu_slots_used", 0)
        gpu_total = capacity.get("gpu_slots_total", 8)
        st.progress(gpu_used / max(gpu_total, 1), text=f"GPU: {gpu_used}/{gpu_total}")

    queue_depth = capacity.get("queue_depth", {})
    if queue_depth:
        st.caption("Queue depth per tenant (Contract C13 — FIFO fair scheduler):")
        for tenant_id, depth in queue_depth.items():
            st.text(f"  Tenant {tenant_id[:8]}…  →  {depth} jobs waiting")


# =============================================================================
# Page: Job Console
# =============================================================================

def render_job_console(token: str):
    st.title("🖥 Job Console")
    st.caption("Live node/phase/iteration progress · Gate summaries · Preview link")

    hdrs = {"Authorization": f"Bearer {token}"}

    # ── Job selector ──────────────────────────────────────────────────────────
    job_id = st.text_input("Job ID", placeholder="Paste a job ID or select from recent jobs")

    try:
        recent = httpx.get(f"{API_BASE}/api/v1/jobs/?limit=10", headers=hdrs, timeout=5)
        recent_jobs = recent.json().get("jobs", []) if recent.status_code == 200 else []
    except Exception:
        recent_jobs = []

    if recent_jobs and not job_id:
        options = [f"{j['id'][:8]}… — {j['status']} — {j.get('spec_name', 'unnamed')}"
                   for j in recent_jobs]
        selected = st.selectbox("Recent jobs", ["— select —"] + options)
        if selected != "— select —":
            job_id = recent_jobs[options.index(selected)]["id"]

    if not job_id:
        st.info("Enter a Job ID above to see live progress.")
        return

    # ── Fetch job + events ────────────────────────────────────────────────────
    try:
        job_r = httpx.get(f"{API_BASE}/api/v1/jobs/{job_id}", headers=hdrs, timeout=5)
        job   = job_r.json() if job_r.status_code == 200 else {}

        events_r = httpx.get(f"{API_BASE}/api/v1/jobs/{job_id}/events", headers=hdrs, timeout=5)
        events   = events_r.json().get("events", []) if events_r.status_code == 200 else []
    except Exception as exc:
        st.error(f"Could not fetch job: {exc}")
        return

    # ── Status badge ──────────────────────────────────────────────────────────
    status = job.get("status", "unknown")
    STATUS_COLOR = {
        "queued": "🔵", "running": "🟡", "completed": "🟢", "failed": "🔴"
    }
    st.markdown(f"## {STATUS_COLOR.get(status, '⚪')} Job `{job_id[:8]}…` — **{status.upper()}**")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Stack",   job.get("stack", "—"))
    with col2:
        st.metric("Created", job.get("created_at", "—")[:19] if job.get("created_at") else "—")
    with col3:
        duration = job.get("duration_s")
        st.metric("Duration", f"{duration:.1f}s" if duration else "running…")

    # ── Live progress bar ─────────────────────────────────────────────────────
    pct = job.get("progress_pct", 0)
    if status == "completed":
        pct = 100
    st.progress(pct / 100, text=f"Progress: {pct}%")

    # ── Event timeline ────────────────────────────────────────────────────────
    st.subheader("📋 Event Timeline")
    PHASE_ICON = {
        "queued": "📥", "started": "🚀", "running": "⚙", "completed": "✅", "failed": "❌"
    }
    for ev in events:
        phase    = ev.get("phase", "")
        node     = ev.get("node", "")
        iteration = ev.get("iteration")
        payload  = ev.get("payload", {})
        ts       = ev.get("created_at", "")[:19]

        icon = PHASE_ICON.get(phase, "▶")
        iter_str = f" iter={iteration}" if iteration is not None else ""
        node_str = f" [{node}]" if node else ""
        st.text(f"{ts}  {icon}  {phase.upper()}{node_str}{iter_str}  {payload}")

    # ── Gate Report Summary ───────────────────────────────────────────────────
    if status in ("completed", "failed"):
        st.subheader("🔬 Gate Report")
        try:
            gate_r = httpx.get(
                f"{SANDBOX_BASE}/report/{job_id}",
                params={"tenant_id": job.get("tenant_id", "")},
                headers=hdrs,
                timeout=10
            )
            if gate_r.status_code == 200:
                gate = gate_r.json()
                overall = gate.get("overall", "unknown")
                badge = "✅ PASS" if overall == "pass" else "❌ FAIL"
                st.markdown(f"### Gate Result: {badge}")

                if gate.get("coverage_pct") is not None:
                    st.metric("Test Coverage", f"{gate['coverage_pct']*100:.1f}%",
                              delta="≥60% required",
                              delta_color="off")

                steps_df = pd.DataFrame(gate.get("steps", []))
                if not steps_df.empty:
                    steps_df["result"] = steps_df["passed"].apply(
                        lambda x: "✅ Pass" if x else "❌ Fail"
                    )
                    st.dataframe(
                        steps_df[["name", "result", "duration_s"]],
                        hide_index=True,
                        use_container_width=True
                    )

                if gate.get("sbom_url"):
                    st.link_button("📦 Download SBOM (CycloneDX)", gate["sbom_url"])

            elif gate_r.status_code == 404:
                st.info("Gate report not yet available.")
            else:
                st.warning(f"Gate report error: {gate_r.status_code}")
        except Exception as exc:
            st.warning(f"Could not fetch gate report: {exc}")

    # ── Preview Link ──────────────────────────────────────────────────────────
    preview_url = job.get("preview_url")
    if preview_url:
        st.divider()
        st.subheader("🌐 Preview")
        st.success(f"Your app is live for 72 hours!")
        st.link_button("Open Preview", preview_url)
        expires_at = job.get("preview_expires_at", "")
        if expires_at:
            st.caption(f"Preview expires: {expires_at[:19]}")

    # ── Auto-refresh for running jobs ─────────────────────────────────────────
    if status == "running":
        time.sleep(3)
        st.rerun()
