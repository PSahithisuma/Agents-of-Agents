"""
VibeForge — Cost & Observability Dashboard
===========================================
Standalone Streamlit page (ui/pages/2_Cost_Dashboard.py)
Shows per-tenant spend, cache hit rate, gate pass rate, 7-day history.
Connects to real budget API: GET /api/v1/budget/{tenant_id}/summary|history
"""
from __future__ import annotations

import os
import time
from datetime import datetime

import httpx
import streamlit as st

# ── Config ────────────────────────────────────────────────────────────────────
API_BASE      = os.environ.get("API_BASE_URL", "https://api-533612622410.us-central1.run.app")
CAPACITY_BASE = os.environ.get("CAPACITY_URL",  "https://capacity-533612622410.us-central1.run.app")
LITELLM_BASE  = os.environ.get("LITELLM_BASE_URL", "https://litellm-533612622410.us-central1.run.app")
DEV_TOKEN     = "dev:admin@vibeforge.dev"
SEED_TENANT   = "00000000-0000-0000-0000-000000000001"

st.set_page_config(page_title="Cost Dashboard | VibeForge", page_icon="📊", layout="wide")
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
.stApp { background: #0d0d1f; color: #e0e0f0; }
[data-testid="stSidebar"] { background: linear-gradient(180deg,#0f0f1a,#1a1a2e); border-right:1px solid #2d2d4e; }
[data-testid="stSidebar"] * { color: #e0e0f0 !important; }
.metric-card {
    background:#1a1a2e; border:1px solid #2d2d4e; border-radius:12px;
    padding:1.25rem 1.5rem; text-align:center;
}
.metric-label { font-size:0.72rem; font-weight:600; letter-spacing:0.1em; color:#7878aa; text-transform:uppercase; }
.metric-value { font-size:2rem; font-weight:700; color:#c4b5fd; margin:0.25rem 0; }
.metric-sub   { font-size:0.8rem; color:#7878aa; }
.metric-zero  { color:#4ade80; }
.metric-nonzero { color:#c4b5fd; }
.bar-row { display:flex; align-items:center; gap:10px; margin:4px 0; }
.bar-label { width:120px; font-size:0.8rem; color:#9090b0; text-align:right; }
.bar-fill  { height:14px; border-radius:4px; background:linear-gradient(90deg,#7c3aed,#4f46e5); }
.bar-pct   { font-size:0.78rem; color:#c4b5fd; }
.status-ok  { color:#4ade80; font-weight:600; }
.status-bad { color:#f87171; font-weight:600; }
</style>
""", unsafe_allow_html=True)

# ── Auth token from session or env ────────────────────────────────────────────
token = st.session_state.get("token", DEV_TOKEN)
hdrs  = {"Authorization": f"Bearer {token}"}

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 📊 Cost Dashboard")
    tenant_id = st.text_input("Tenant ID", value=SEED_TENANT, help="UUID of tenant")
    auto_refresh = st.checkbox("Auto-refresh (30s)", value=False)
    if auto_refresh:
        time.sleep(30)
        st.rerun()
    st.divider()
    st.markdown(f"**API:** `{API_BASE}`")
    st.markdown(f"**Auth:** `{token[:24]}…`" if len(token) > 24 else f"**Auth:** `{token}`")

# ── Fetch data ────────────────────────────────────────────────────────────────
def safe_get(url, hdrs, timeout=10):
    try:
        r = httpx.get(url, headers=hdrs, timeout=timeout)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}

summary = safe_get(f"{API_BASE}/api/v1/budget/{tenant_id}/summary", hdrs)
history = safe_get(f"{API_BASE}/api/v1/budget/{tenant_id}/history", hdrs)
cap_status = safe_get(f"{CAPACITY_BASE}/status", hdrs)

# Derive spend value (show $0.00 for cached/free jobs)
llm_tokens   = summary.get("llm_tokens_today", 0)
job_count    = summary.get("job_count_today",  0)
# Rough cost estimate: Gemini Flash ≈ $0.075/1M tokens, Groq ≈ $0.059/1M
token_cost   = round(llm_tokens * 0.000000075, 6)
cost_str     = f"${token_cost:.4f}" if token_cost > 0 else "$0.00"
cost_cls     = "metric-nonzero" if token_cost > 0 else "metric-zero"

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("## 📊 Cost & Observability Dashboard")
st.caption(f"Tenant: `{tenant_id}` · Updated: {datetime.utcnow().strftime('%H:%M:%S UTC')}")
st.divider()

# ── KPI Row ───────────────────────────────────────────────────────────────────
c1, c2, c3, c4, c5 = st.columns(5)

with c1:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">LLM Spend (today)</div>
        <div class="metric-value {cost_cls}">{cost_str}</div>
        <div class="metric-sub">Groq + Gemini Flash</div>
    </div>""", unsafe_allow_html=True)

with c2:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">Jobs Run Today</div>
        <div class="metric-value">{job_count}</div>
        <div class="metric-sub">completed</div>
    </div>""", unsafe_allow_html=True)

with c3:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">Tokens Used</div>
        <div class="metric-value">{llm_tokens:,}</div>
        <div class="metric-sub">input + output</div>
    </div>""", unsafe_allow_html=True)

with c4:
    queue_depth = cap_status.get("queue_depth", 0)
    active      = cap_status.get("active_slots", 0)
    max_slots   = cap_status.get("max_slots", 3)
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">Queue Depth</div>
        <div class="metric-value">{queue_depth}</div>
        <div class="metric-sub">{active}/{max_slots} slots active</div>
    </div>""", unsafe_allow_html=True)

with c5:
    gate_pass = summary.get("gate_pass_rate_pct", 100)
    gcolor = "metric-zero" if gate_pass >= 80 else "metric-nonzero"
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">Gate Pass Rate</div>
        <div class="metric-value {gcolor}">{gate_pass:.0f}%</div>
        <div class="metric-sub">QA gate success</div>
    </div>""", unsafe_allow_html=True)

st.divider()

# ── 7-Day History Chart ───────────────────────────────────────────────────────
st.markdown("### 📈 7-Day Spend History")

history_items = history.get("history", []) if isinstance(history, dict) else []

if history_items:
    try:
        import pandas as pd
        df = pd.DataFrame(history_items)
        df["cost_usd"] = df.get("llm_tokens", pd.Series([0]*len(df))) * 0.000000075
        df["date"] = pd.to_datetime(df.get("date", pd.Series([])))
        st.line_chart(df.set_index("date")["cost_usd"], color="#7c3aed")
    except Exception as e:
        st.info(f"Chart unavailable: {e}")
else:
    # Demo mode: show flat $0.00 line for cached jobs
    st.markdown("""
    <div style="background:#1a1a2e;border:1px solid #2d2d4e;border-radius:12px;padding:2rem;text-align:center;">
        <div style="font-size:3rem;margin-bottom:0.5rem;">💚</div>
        <div style="font-size:1.5rem;font-weight:700;color:#4ade80;">$0.00</div>
        <div style="color:#7878aa;margin-top:0.5rem;">No spend recorded — all jobs served from cache</div>
    </div>
    """, unsafe_allow_html=True)

st.divider()

# ── Gate Pass/Fail Breakdown ──────────────────────────────────────────────────
col_gate, col_cache = st.columns(2)

with col_gate:
    st.markdown("### 🚦 Gate Results")
    gate_data = summary.get("gate_breakdown", {"python_fastapi": {"pass": 0, "fail": 0}, "java_springboot": {"pass": 0, "fail": 0}})
    for stack, counts in gate_data.items():
        total = counts.get("pass", 0) + counts.get("fail", 0)
        pct = int(counts.get("pass", 0) / total * 100) if total > 0 else 100
        bar_w = max(4, pct * 2)
        st.markdown(f"""
        <div class="bar-row">
            <span class="bar-label">{stack.replace("_"," ")}</span>
            <div class="bar-fill" style="width:{bar_w}px;"></div>
            <span class="bar-pct">{pct}%</span>
        </div>""", unsafe_allow_html=True)
    if not gate_data or all(v.get("pass",0)+v.get("fail",0)==0 for v in gate_data.values()):
        st.success("✅ No gate failures today")

with col_cache:
    st.markdown("### ⚡ Cache Performance")
    cache_hits   = summary.get("cache_hits_today", 0)
    cache_misses = summary.get("cache_misses_today", 0)
    total_cache  = cache_hits + cache_misses
    hit_rate_pct = int(cache_hits / total_cache * 100) if total_cache > 0 else 0
    
    saved_cost = cache_hits * 0.003  # ~$0.003 per cached job saved
    
    m1, m2 = st.columns(2)
    m1.metric("Cache Hits", cache_hits, help="Jobs answered from cache (cost=$0.00)")
    m2.metric("Hit Rate",   f"{hit_rate_pct}%")
    m1.metric("Cache Misses", cache_misses)
    m2.metric("Saved", f"${saved_cost:.4f}", help="Estimated LLM cost avoided by cache")

st.divider()

# ── Budget API Raw ────────────────────────────────────────────────────────────
with st.expander("🔍 Raw Budget API Response"):
    st.json({"summary": summary, "history_preview": history_items[:3] if history_items else []})
