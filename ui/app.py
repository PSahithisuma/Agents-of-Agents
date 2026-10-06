"""
VibeForge UI — Streamlit App  (v2)
====================================
Pages:
  Login              — Keycloak OIDC (password grant) with dev fallback
  Spec Sheet         — Option graph rendered from ecommerce YAML, presets,
                       conflict warnings, live impact panel
  Review Workspace   — Spec diff cards, amendment text-boxes, accept/reject,
                       Lock and Generate -> SSE job progress
  Job Console        — Live node phases, gate summaries, bundle download

Auth strategy:
  1. Try Keycloak Resource Owner Password Credentials grant.
  2. If KC unavailable, fall back to dev:email token (accepted by API bypass).
  3. Token stored in st.session_state and refreshed every 4 min.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

import httpx
import streamlit as st

# ──────────────────────────────────────────────────────────────────────────────
# Configuration (from env vars set at Cloud Run deploy time)
# ──────────────────────────────────────────────────────────────────────────────
API_BASE         = os.getenv("API_BASE_URL",     "https://api-533612622410.us-central1.run.app")
KC_BASE          = os.getenv("KEYCLOAK_URL",     "https://keycloak-533612622410.us-central1.run.app/auth")
KC_REALM         = os.getenv("KEYCLOAK_REALM",   "vibeforge")
KC_CLIENT_ID     = os.getenv("KEYCLOAK_CLIENT_ID", "vibeforge-app")
DEV_FALLBACK_TOKEN = "dev:test@vibeforge.dev"
TOKEN_TTL_SECS   = 240   # refresh before 5-min Keycloak default



# ──────────────────────────────────────────────────────────────────────────────
# Page config + custom CSS
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="VibeForge",
    page_icon="🔥",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
/* ── Global ── */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

/* ── Sidebar ── */
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0f0f1a 0%, #1a1a2e 100%);
    border-right: 1px solid #2d2d4e;
}
[data-testid="stSidebar"] * { color: #e0e0f0 !important; }

/* ── Main area ── */
.stApp { background: #0d0d1f; color: #e0e0f0; }

/* ── Cards ── */
.vf-card {
    background: #1a1a2e;
    border: 1px solid #2d2d4e;
    border-radius: 12px;
    padding: 1.25rem 1.5rem;
    margin-bottom: 1rem;
}
.vf-card-title {
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #7878aa;
    margin-bottom: 0.75rem;
}
.vf-card-highlight {
    border-left: 3px solid #7c3aed;
}

/* ── Impact pill ── */
.vf-pill {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 500;
    margin: 2px 3px 2px 0;
}
.vf-pill-purple { background: #4c1d95; color: #c4b5fd; }
.vf-pill-red    { background: #7f1d1d; color: #fca5a5; }
.vf-pill-green  { background: #14532d; color: #86efac; }
.vf-pill-blue   { background: #1e3a5f; color: #93c5fd; }
.vf-pill-yellow { background: #713f12; color: #fde68a; }

/* ── Hash display ── */
.vf-hash {
    font-family: 'Courier New', monospace;
    font-size: 0.75rem;
    color: #7c3aed;
    background: #1e1b4b;
    padding: 4px 10px;
    border-radius: 6px;
    letter-spacing: 0.05em;
}

/* ── Progress bar ── */
.vf-progress-label {
    font-size: 0.8rem;
    color: #a0a0c0;
    margin-top: 4px;
}

/* ── Buttons ── */
div.stButton > button {
    background: linear-gradient(135deg, #7c3aed 0%, #4f46e5 100%);
    color: white;
    border: none;
    border-radius: 8px;
    padding: 0.5rem 1.5rem;
    font-weight: 600;
    font-size: 0.9rem;
    transition: opacity 0.2s;
    width: 100%;
}
div.stButton > button:hover { opacity: 0.85; }

/* ── Section header ── */
.vf-section-header {
    font-size: 1.5rem;
    font-weight: 700;
    color: #e0e0f0;
    margin-bottom: 0.25rem;
}
.vf-section-sub {
    font-size: 0.85rem;
    color: #7878aa;
    margin-bottom: 1.5rem;
}

/* ── Spec field row ── */
.vf-field-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 0;
    border-bottom: 1px solid #2d2d4e;
    font-size: 0.85rem;
}
.vf-field-key   { color: #9090b0; }
.vf-field-value { color: #e0e0f0; font-weight: 500; }
.vf-field-changed { color: #c4b5fd; font-weight: 600; }

/* ── AC list ── */
.vf-ac-item {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 5px 0;
    font-size: 0.82rem;
    color: #c0c0e0;
}
.vf-ac-id { color: #7c3aed; font-weight: 600; min-width: 52px; }
</style>
""", unsafe_allow_html=True)



# ══════════════════════════════════════════════════════════════════════════════
# Auth helpers
# ══════════════════════════════════════════════════════════════════════════════

def _keycloak_token_url() -> str:
    return f"{KC_BASE}/realms/{KC_REALM}/protocol/openid-connect/token"


def _do_keycloak_login(username: str, password: str) -> tuple[str | None, str]:
    """Attempt Keycloak ROPC grant. Returns (access_token, error_message)."""
    try:
        resp = httpx.post(
            _keycloak_token_url(),
            data={
                "client_id":  KC_CLIENT_ID,
                "username":   username,
                "password":   password,
                "grant_type": "password",
                "scope":      "openid profile email",
            },
            timeout=8.0,
        )
        if resp.status_code == 200:
            return resp.json().get("access_token"), ""
        body = resp.json()
        err = body.get("error_description") or body.get("error") or resp.text[:200]
        return None, err
    except Exception as exc:
        return None, f"Keycloak unreachable: {exc}"


def _token_age() -> float:
    return time.time() - st.session_state.get("token_time", 0)


def _get_token() -> str | None:
    """Return cached token; clears if expired (Keycloak mode)."""
    if "token" in st.session_state and _token_age() < TOKEN_TTL_SECS:
        return st.session_state["token"]
    if st.session_state.get("auth_mode") == "keycloak":
        st.session_state.pop("token", None)
        return None
    return st.session_state.get("token")


def _headers() -> dict[str, str]:
    tok = _get_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _is_logged_in() -> bool:
    return bool(st.session_state.get("token"))


# ══════════════════════════════════════════════════════════════════════════════
# Login page
# ══════════════════════════════════════════════════════════════════════════════

def page_login() -> None:
    col_l, col_c, col_r = st.columns([1, 2, 1])
    with col_c:
        st.markdown("""
        <div style="text-align:center;padding:2rem 0 1.5rem;">
            <div style="font-size:3.5rem;">🔥</div>
            <div style="font-size:1.8rem;font-weight:700;color:#c4b5fd;">VibeForge</div>
            <div style="font-size:0.9rem;color:#7878aa;margin-top:0.5rem;">Spec → Code Platform</div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("#### Login with VibeForge")
        username = st.text_input("Email / Username", placeholder="you@example.com", key="login_user")
        password = st.text_input("Password", type="password", key="login_pass")

        if st.button("🔑 Login with VibeForge", key="login_btn"):
            if not username or not password:
                st.error("Enter username and password")
            else:
                with st.spinner("Authenticating with Keycloak…"):
                    token, err = _do_keycloak_login(username, password)

                if token:
                    st.session_state.update({
                        "token": token, "token_time": time.time(),
                        "auth_mode": "keycloak", "user_email": username,
                    })
                    st.success("✓ Logged in via Keycloak")
                    st.rerun()
                else:
                    # Keycloak failed → fall back to dev token
                    st.warning(f"Keycloak: {err}\n\nFalling back to dev token mode.")
                    st.session_state.update({
                        "token": DEV_FALLBACK_TOKEN, "token_time": time.time(),
                        "auth_mode": "dev", "user_email": username or "dev@vibeforge.dev",
                    })
                    time.sleep(1)
                    st.rerun()

        st.divider()
        if st.button("⚡ Continue as Dev (skip login)", key="dev_btn"):
            st.session_state.update({
                "token": DEV_FALLBACK_TOKEN, "token_time": time.time(),
                "auth_mode": "dev", "user_email": "dev@vibeforge.dev",
            })
            st.rerun()

        st.caption(f"Keycloak: `{KC_BASE}` · Realm: `{KC_REALM}` · Client: `{KC_CLIENT_ID}`")


# ══════════════════════════════════════════════════════════════════════════════
# API helpers
# ══════════════════════════════════════════════════════════════════════════════

def api_get(path: str, params: dict | None = None) -> Any:
    try:
        r = httpx.get(f"{API_BASE}{path}", headers=_headers(), params=params, timeout=12.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        st.error(f"API {e.response.status_code}: {e.response.text[:300]}")
        return None
    except Exception as e:
        st.error(f"API connection error: {e}")
        return None


def api_post(path: str, body: dict) -> Any:
    try:
        r = httpx.post(f"{API_BASE}{path}", json=body, headers=_headers(), timeout=30.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        st.error(f"API {e.response.status_code}: {e.response.text[:400]}")
        return None
    except Exception as e:
        st.error(f"API connection error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Option-graph data (embedded from packs/ecommerce/option_graphs/main.yaml)
# ══════════════════════════════════════════════════════════════════════════════

OPTION_GRAPH: list[dict] = [
    {
        "section_id": "business_models",
        "label": "Business Model",
        "description": "Select all that apply. Incompatible combinations will be flagged.",
        "prerequisites": [],
        "options": [
            {"option_id": "biz_b2c", "label": "B2C — direct to consumer",
             "description": "Sell products directly to end consumers.", "default": True,
             "conflicts": [], "requires": [],
             "impact": {"entities": 1, "endpoints": 4, "screens": 3}},
            {"option_id": "biz_b2b", "label": "B2B — business accounts",
             "description": "Sell to business customers with account management.", "default": False,
             "conflicts": [], "requires": [],
             "impact": {"entities": 2, "endpoints": 6, "screens": 2}},
            {"option_id": "biz_quick_commerce", "label": "Quick commerce — dark store model",
             "description": "10–30 minute delivery from local inventory nodes.", "default": False,
             "conflicts": ["biz_b2b"], "requires": ["biz_b2c"],
             "impact": {"entities": 3, "endpoints": 5, "screens": 2}},
            {"option_id": "biz_c2c", "label": "C2C — consumer marketplace",
             "description": "Platform where consumers buy and sell from each other.", "default": False,
             "conflicts": [], "requires": ["biz_b2c"],
             "impact": {"entities": 2, "endpoints": 8, "screens": 4}},
        ],
    },
    {
        "section_id": "catalog",
        "label": "Catalog & Products",
        "description": "Product catalog features.",
        "prerequisites": ["biz_b2c"],
        "options": [
            {"option_id": "cat_variants", "label": "Product variants (size, color, etc.)",
             "description": "Multiple variants with different prices and stock.", "default": False,
             "conflicts": [], "requires": [],
             "impact": {"entities": 1, "endpoints": 4, "screens": 1}},
            {"option_id": "cat_search", "label": "Product search and filters",
             "description": "Full-text search with category, price, rating filters.", "default": False,
             "conflicts": [], "requires": [], "impact": {"endpoints": 1}},
            {"option_id": "cat_categories", "label": "Category hierarchy",
             "description": "Nested product categories (up to 3 levels).", "default": False,
             "conflicts": [], "requires": [],
             "impact": {"entities": 1, "endpoints": 3}},
        ],
    },
    {
        "section_id": "payments",
        "label": "Payments",
        "description": "Payment gateway integrations and checkout options.",
        "prerequisites": ["biz_b2c"],
        "options": [
            {"option_id": "pay_razorpay", "label": "Razorpay integration",
             "description": "Accept cards, UPI, net banking, wallets via Razorpay.", "default": False,
             "conflicts": [], "requires": [],
             "impact": {"endpoints": 3, "integrations": 1}},
            {"option_id": "pay_guest_checkout", "label": "Guest checkout (no account required)",
             "description": "Allow purchases without creating an account.", "default": False,
             "conflicts": ["biz_b2b"], "requires": ["biz_b2c"],
             "impact": {"endpoints": 2, "screens": 1}},
            {"option_id": "pay_saved_methods", "label": "Saved payment methods",
             "description": "Let logged-in users save cards and bank accounts.", "default": False,
             "conflicts": [], "requires": [],
             "impact": {"entities": 1, "endpoints": 4}},
        ],
    },
    {
        "section_id": "compliance",
        "label": "Compliance",
        "description": "Regulatory compliance frameworks.",
        "prerequisites": [],
        "options": [
            {"option_id": "comp_gdpr", "label": "GDPR data privacy",
             "description": "Right to erasure, data export, consent tracking for EU customers.", "default": False,
             "conflicts": [], "requires": [],
             "impact": {"endpoints": 2, "compliance_rules": 3}},
            {"option_id": "comp_gst", "label": "GST e-invoicing (India)",
             "description": "Generate IRN-compliant e-invoices for B2B transactions above Rs5 lakh.", "default": False,
             "conflicts": [], "requires": ["biz_b2b"],
             "impact": {"endpoints": 2, "integrations": 1, "compliance_rules": 3}},
            {"option_id": "comp_pci", "label": "PCI-DSS alignment",
             "description": "PCI-DSS controls — PAN tokenisation, no-PAN-in-logs Semgrep rules.", "default": False,
             "conflicts": [], "requires": ["pay_razorpay"],
             "impact": {"compliance_rules": 5}},
        ],
    },
]

PRESETS: dict[str, set] = {
    "Startup":       {"biz_b2c", "cat_variants", "cat_search", "pay_razorpay", "pay_guest_checkout"},
    "Enterprise":    {"biz_b2c", "biz_b2b", "cat_variants", "cat_search", "cat_categories",
                      "pay_razorpay", "pay_saved_methods", "comp_gdpr", "comp_gst", "comp_pci"},
    "Marketplace":   {"biz_b2c", "biz_c2c", "cat_variants", "cat_search", "cat_categories",
                      "pay_razorpay", "pay_guest_checkout", "comp_gdpr"},
    "Quick Commerce":{"biz_b2c", "biz_quick_commerce", "cat_search",
                      "pay_razorpay", "pay_guest_checkout"},
}
PRESET_ICONS = {"Startup": "🚀", "Enterprise": "🏢", "Marketplace": "🛒", "Quick Commerce": "⚡"}


def _all_option_ids() -> set[str]:
    return {opt["option_id"] for sec in OPTION_GRAPH for opt in sec["options"]}


def _label(oid: str) -> str:
    for sec in OPTION_GRAPH:
        for opt in sec["options"]:
            if opt["option_id"] == oid:
                return opt["label"]
    return oid


def _check_conflicts(selected: set[str]) -> list[str]:
    warnings: list[str] = []
    for sec in OPTION_GRAPH:
        for opt in sec["options"]:
            oid = opt["option_id"]
            if oid not in selected:
                continue
            for conflict in opt.get("conflicts", []):
                if conflict in selected:
                    warnings.append(f"⚠ **{opt['label']}** conflicts with **{_label(conflict)}**")
            for req in opt.get("requires", []):
                if req not in selected:
                    warnings.append(f"⚠ **{opt['label']}** requires **{_label(req)}** — not selected")
    return list(dict.fromkeys(warnings))


def _compute_live_impact(selected: set[str]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for sec in OPTION_GRAPH:
        for opt in sec["options"]:
            if opt["option_id"] in selected:
                for k, v in opt.get("impact", {}).items():
                    totals[k] = totals.get(k, 0) + v
    return totals


def _section_unlocked(section: dict, selected: set[str]) -> bool:
    return all(p in selected for p in section.get("prerequisites", []))


# ══════════════════════════════════════════════════════════════════════════════
# HTML helpers
# ══════════════════════════════════════════════════════════════════════════════

def _pill(text: str, colour: str = "purple") -> str:
    return f'<span class="vf-pill vf-pill-{colour}">{text}</span>'


def _field_row(key: str, value: str, changed: bool = False) -> str:
    val_cls = "vf-field-changed" if changed else "vf-field-value"
    return (
        f'<div class="vf-field-row">'
        f'<span class="vf-field-key">{key}</span>'
        f'<span class="{val_cls}">{value}</span>'
        f'</div>'
    )


def _spec_card(title: str, rows: list[str], highlight: bool = False) -> None:
    cls = "vf-card vf-card-highlight" if highlight else "vf-card"
    st.markdown(
        f'<div class="{cls}"><div class="vf-card-title">{title}</div>{"".join(rows)}</div>',
        unsafe_allow_html=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Page 1: Spec Sheet
# ══════════════════════════════════════════════════════════════════════════════

def page_spec_sheet() -> None:
    st.markdown('<div class="vf-section-header">🧬 Spec Sheet</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="vf-section-sub">Choose your stack options. '
        'The Impact Panel updates live as you select.</div>',
        unsafe_allow_html=True,
    )

    # Project selector
    projects_data = api_get("/api/v1/projects/")
    projects = (
        projects_data.get("items", []) if isinstance(projects_data, dict)
        else (projects_data or [])
    )
    proj_names = [p["name"] for p in projects]
    proj_ids   = [p["id"]   for p in projects]

    col_proj, col_new = st.columns([3, 1])
    with col_proj:
        selected_proj_name = st.selectbox(
            "Project", proj_names if proj_names else ["— no projects —"], key="proj_selector"
        )
    with col_new:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("+ New Project", key="new_proj_btn"):
            st.session_state["creating_project"] = True

    if st.session_state.get("creating_project"):
        with st.form("new_project_form"):
            new_name = st.text_input("Project name", placeholder="My App")
            new_desc = st.text_input("Description (optional)")
            if st.form_submit_button("Create") and new_name:
                res = api_post("/api/v1/projects/", {"name": new_name, "description": new_desc})
                if res:
                    st.success(f"Created: {res.get('id','?')}")
                    st.session_state["creating_project"] = False
                    st.rerun()

    project_id: str | None = None
    if proj_ids and selected_proj_name in proj_names:
        project_id = proj_ids[proj_names.index(selected_proj_name)]

    st.divider()

    # Preset quick-fill buttons
    st.markdown("**Quick Presets**")
    preset_cols = st.columns(len(PRESETS))
    for col, (pname, pset) in zip(preset_cols, PRESETS.items()):
        with col:
            icon = PRESET_ICONS.get(pname, "")
            if st.button(f"{icon} {pname}", key=f"preset_{pname}"):
                for oid in _all_option_ids():
                    st.session_state[f"opt_{oid}"] = oid in pset
                st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)

    # Layout: option form (2/3) + impact panel (1/3)
    form_col, impact_col = st.columns([2, 1], gap="large")

    with form_col:
        selected: set[str] = set()

        for section in OPTION_GRAPH:
            unlocked = _section_unlocked(section, selected)
            prereqs  = section.get("prerequisites", [])
            prereq_note = (
                f" *(requires: {', '.join(_label(p) for p in prereqs)})*"
                if prereqs and not unlocked else ""
            )

            with st.expander(f"**{section['label']}**{prereq_note}", expanded=unlocked):
                st.caption(section.get("description", ""))
                for opt in section["options"]:
                    oid = opt["option_id"]
                    default_val = st.session_state.get(f"opt_{oid}", opt.get("default", False))
                    checked = st.checkbox(
                        f"**{opt['label']}**",
                        value=default_val,
                        key=f"opt_{oid}",
                        disabled=not unlocked,
                        help=opt.get("description", ""),
                    )
                    if checked:
                        selected.add(oid)
                    impact_hints = opt.get("impact", {})
                    if impact_hints:
                        hint = "  ·  ".join(f"+{v} {k}" for k, v in impact_hints.items())
                        st.caption(f"  ↳ {hint}")

        # Conflict warnings
        conflicts = _check_conflicts(selected)
        for w in conflicts:
            st.markdown(f'<div class="vf-conflict">{w}</div>', unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        app_name    = st.text_input("App Name", value="MyEcomApp", key="app_name")
        description = st.text_area("Description (optional)", key="app_desc", height=60)

        if st.button("📐 Compile Draft", key="compile_btn", disabled=not project_id):
            with st.spinner("Compiling spec…"):
                result = api_post(
                    f"/api/v1/projects/{project_id}/specs/",
                    {"selections": {}, "app_name": app_name, "description": description, "vertical": "ecommerce"},
                )
            if result:
                st.session_state["last_spec"]     = result
                st.session_state["last_proj_id"]  = project_id
                st.session_state["last_selected"] = list(selected)
                hsh = result.get("canonical_hash", "")[:16]
                st.success(
                    f"✓ Draft v{result.get('version',1)} compiled — hash `{hsh}…` "
                    "· Go to **Review Workspace** to lock."
                )
        if not project_id:
            st.caption("⚠ Select or create a project first")

    # Live Impact Panel
    with impact_col:
        st.markdown("#### Live Impact Panel")

        preview_hash = hashlib.sha256(
            json.dumps(sorted(selected), separators=(",", ":")).encode()
        ).hexdigest()[:16]
        st.markdown(f'<div class="vf-hash">#{preview_hash}…</div>', unsafe_allow_html=True)
        st.caption("Preview hash (updates live)")

        st.markdown("<br>", unsafe_allow_html=True)

        live_impact = _compute_live_impact(selected)
        col_a, col_b = st.columns(2)
        col_a.metric("Entities",     live_impact.get("entities", 0))
        col_b.metric("Endpoints",    live_impact.get("endpoints", 0))
        col_c, col_d = st.columns(2)
        col_c.metric("Screens",      live_impact.get("screens", 0))
        col_d.metric("Integrations", live_impact.get("integrations", 0))
        if live_impact.get("compliance_rules", 0):
            st.metric("Compliance Rules", live_impact["compliance_rules"])

        st.markdown("<br>", unsafe_allow_html=True)

        stack_map = [
            ("biz_b2c", "B2C", "blue"), ("biz_b2b", "B2B", "purple"),
            ("biz_quick_commerce", "Quick Commerce", "orange"),
            ("biz_c2c", "C2C Marketplace", "purple"),
            ("pay_razorpay", "Razorpay", "green"),
            ("comp_gdpr", "GDPR", "yellow"),
            ("comp_gst", "GST e-Invoice", "yellow"),
            ("comp_pci", "PCI-DSS", "red"),
        ]
        pills_html = "".join(
            f'<span class="vf-pill vf-pill-{c}">{t}</span>'
            for oid, t, c in stack_map if oid in selected
        )
        if pills_html:
            st.markdown(f"**Stack:** {pills_html}", unsafe_allow_html=True)

        if conflicts:
            st.markdown(
                f'<span class="vf-pill vf-pill-red">⚠ {len(conflicts)} conflict(s)</span>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<span class="vf-pill vf-pill-green">✓ No conflicts</span>',
                unsafe_allow_html=True,
            )


# ══════════════════════════════════════════════════════════════════════════════
# Page 2: Review Workspace
# ══════════════════════════════════════════════════════════════════════════════

def page_review_workspace() -> None:
    st.markdown('<div class="vf-section-header">🔍 Review Workspace</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="vf-section-sub">Review compiled spec, propose amendments, and lock to generate.</div>',
        unsafe_allow_html=True,
    )

    # Project selector
    projects_data = api_get("/api/v1/projects/")
    projects = (
        projects_data.get("items", []) if isinstance(projects_data, dict)
        else (projects_data or [])
    )
    proj_names = [p["name"] for p in projects]
    proj_ids   = [p["id"]   for p in projects]

    last_spec    = st.session_state.get("last_spec")
    last_proj_id = st.session_state.get("last_proj_id")

    selected_proj_name = st.selectbox(
        "Project", proj_names if proj_names else ["— no projects —"], key="review_proj"
    )
    project_id: str | None = None
    if proj_ids and selected_proj_name in proj_names:
        project_id = proj_ids[proj_names.index(selected_proj_name)]

    if project_id and project_id != last_proj_id:
        specs = api_get(f"/api/v1/projects/{project_id}/specs/")
        if specs and len(specs) > 0:
            last_spec = specs[-1]
            st.session_state["last_spec"]    = last_spec
            st.session_state["last_proj_id"] = project_id

    if not last_spec:
        st.info("No spec compiled yet. Go to **Spec Sheet**, set options, and click **Compile Draft**.")
        return

    spec_data  = last_spec.get("spec_data", {})
    canon_hash = last_spec.get("canonical_hash", "")
    version    = last_spec.get("version", 1)
    is_frozen  = last_spec.get("frozen_at") is not None

    hdr_col, status_col = st.columns([3, 1])
    with hdr_col:
        st.markdown(
            f"**Spec v{version}** &nbsp; <span class=\"vf-hash\">#{canon_hash[:24]}…</span>",
            unsafe_allow_html=True,
        )
    with status_col:
        if is_frozen:
            st.success("🔒 Frozen", icon="✅")
        else:
            st.warning("📝 Draft", icon="⚠️")

    st.divider()

    # Amendment state
    selected_ids: list[str] = st.session_state.get("last_selected", [])
    selected_set = set(selected_ids)
    amendments: dict[str, dict] = st.session_state.setdefault("amendments", {})
    accepted:   set[str]        = st.session_state.setdefault("accepted_amendments", set())
    rejected:   set[str]        = st.session_state.setdefault("rejected_amendments", set())

    # Spec sections with amendment text-boxes + diff cards
    st.markdown("#### Spec Sections (with Amendment Notes)")
    for section in OPTION_GRAPH:
        with st.expander(f"📂 {section['label']}", expanded=True):
            for opt in section["options"]:
                oid    = opt["option_id"]
                is_sel = oid in selected_set
                badge  = _pill("✓ selected", "green") if is_sel else _pill("not selected", "blue")

                col_opt, col_amend = st.columns([2, 1])
                with col_opt:
                    st.markdown(
                        f"{badge} &nbsp; **{opt['label']}**<br>"
                        f'<span style="font-size:0.78rem;color:#7878aa;">{opt["description"]}</span>',
                        unsafe_allow_html=True,
                    )
                with col_amend:
                    current_amend = amendments.get(oid, {}).get("text", "")
                    new_text = st.text_area(
                        "Amendment",
                        value=current_amend,
                        key=f"amend_{oid}",
                        height=60,
                        placeholder="Propose a change…",
                        label_visibility="collapsed",
                    )
                    if new_text != current_amend:
                        amendments[oid] = {"text": new_text, "original_selected": is_sel}

                    # Diff card
                    if new_text:
                        old_val = "selected" if is_sel else "not selected"
                        st.markdown(
                            f'<div class="vf-card" style="padding:0.5rem 0.75rem;margin-top:4px;">'
                            f'<span class="vf-diff-old">Old: {old_val}</span><br>'
                            f'<span class="vf-diff-new">→ {new_text[:80]}</span>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                        a_col, r_col = st.columns(2)
                        with a_col:
                            if st.button("✅ Accept", key=f"accept_{oid}"):
                                accepted.add(oid)
                                rejected.discard(oid)
                        with r_col:
                            if st.button("❌ Reject", key=f"reject_{oid}"):
                                rejected.add(oid)
                                accepted.discard(oid)
                                amendments.pop(oid, None)

                    if oid in accepted:
                        st.success("Amendment accepted")
                    elif oid in rejected:
                        st.error("Amendment rejected")

    # Spec overview cards
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("#### Spec Overview")

    security = spec_data.get("security", {})
    nfr      = spec_data.get("nfr", {})
    api_m    = spec_data.get("api_model", {})
    domain   = spec_data.get("domain_model", {})
    comp_fw  = spec_data.get("compliance_model", {}).get("frameworks", [])
    impact   = _compute_live_impact(selected_set)

    c1, c2 = st.columns(2)
    with c1:
        _spec_card(
            "🗃 Domain Model",
            [
                _field_row("entities",  str(impact.get("entities", 0) + 5)),
                _field_row("endpoints", str(impact.get("endpoints", 0) + 8)),
                _field_row("screens",   str(impact.get("screens", 0) + 2)),
                _field_row("db",        domain.get("db_tier", "postgres")),
            ],
        )
        _spec_card(
            "⚡ API Model",
            [
                _field_row("style",      api_m.get("style", "REST")),
                _field_row("versioning", api_m.get("versioning", "url-prefix")),
                _field_row("pagination", api_m.get("pagination", "cursor")),
            ],
        )

    with c2:
        has_comp = bool(comp_fw)
        _spec_card(
            "🔐 Security & Compliance",
            [
                _field_row("auth",         security.get("auth_strategy", "JWT")),
                _field_row("frameworks",   ", ".join(comp_fw) if comp_fw else "none", has_comp),
                _field_row("mfa",          str(security.get("mfa_required", False))),
                _field_row("integrations", str(impact.get("integrations", 0))),
            ],
            highlight=has_comp,
        )
        _spec_card(
            "🚀 Deploy & NFR",
            [
                _field_row("deploy",           nfr.get("deploy_target", "Cloud Run")),
                _field_row("compliance rules", str(impact.get("compliance_rules", 0))),
                _field_row("slo p99",          f"{nfr.get('response_time_slo_ms', 500)}ms"),
                _field_row("availability",     f"{nfr.get('availability_slo', 99.5)}%"),
            ],
        )

    # Acceptance criteria
    ac_list = spec_data.get("acceptance", [])
    if ac_list:
        st.markdown("#### Acceptance Criteria")
        ac_html = "".join(
            f'<div class="vf-ac-item">'
            f'<span class="vf-ac-id">{ac.get("id","AC-?")}</span>'
            f'<span>{ac.get("description","")}</span>'
            f'</div>'
            for ac in ac_list
        )
        st.markdown(f'<div class="vf-card">{ac_html}</div>', unsafe_allow_html=True)

    st.divider()

    # Lock & Generate
    st.markdown("#### 🔒 Lock & Generate")
    st.markdown(
        "Locking freezes this spec and enqueues a generation job. "
        "Identical specs skip generation via the P3 cache (canonical hash match)."
    )
    if accepted:
        st.info(f"📝 {len(accepted)} accepted amendment(s) will be included in generation context.")
    if is_frozen:
        st.info("Spec already locked. Check Job Console.")

    if st.button(
        "🔒 Lock & Generate" if not is_frozen else "✅ Already Locked",
        key="lock_btn",
        disabled=is_frozen or not project_id,
    ):
        with st.spinner("Locking spec and enqueuing generation job…"):
            payload: dict[str, Any] = {
                "selections":  {},
                "vertical":    "ecommerce",
                "app_name":    spec_data.get("app_name", "MyEcomApp"),
                "description": spec_data.get("description", ""),
                "amendments":  {k: v["text"] for k, v in amendments.items() if k in accepted},
            }
            result = api_post(f"/api/v1/projects/{project_id}/generate", payload)

        if result:
            job_id    = result.get("job_id")
            cache_hit = result.get("cache_hit", False)
            st.session_state["active_job_id"]  = job_id
            st.session_state["active_proj_id"] = project_id
            if cache_hit:
                st.success(f"♻ Cache hit — existing job `{job_id}`")
            else:
                st.success(f"✓ Job `{job_id}` enqueued — open **Job Console** to track.")

    # Inline progress if job was just triggered
    active_job_id = st.session_state.get("active_job_id")
    if active_job_id:
        st.markdown("#### Job Progress")
        _render_job_progress(active_job_id, compact=True)


# ══════════════════════════════════════════════════════════════════════════════
# Page 3: Job Console
# ══════════════════════════════════════════════════════════════════════════════

STATUS_ICONS = {
    "queued": "🟡", "running": "🔵", "completed": "🟢",
    "failed": "🔴", "cancelled": "⚫",
}


def _render_job_progress(job_id: str, compact: bool = False) -> None:
    """Render job status, progress bar, event log, gate summaries, and download button."""
    job = api_get(f"/api/v1/jobs/{job_id}")
    if not job:
        st.warning("Cannot fetch job status")
        return

    status = job.get("status", "unknown")
    icon   = STATUS_ICONS.get(status, "⚪")

    col_status, col_id = st.columns([2, 3])
    with col_status:
        st.markdown(f"**Status:** {icon} `{status}`")
    with col_id:
        st.markdown(f'<span class="vf-hash">{job_id}</span>', unsafe_allow_html=True)

    events = api_get(f"/api/v1/jobs/{job_id}/events") or []
    progress_evts = [e for e in events if e.get("event_type") == "progress"]
    if progress_evts:
        last_pct = progress_evts[-1].get("payload", {}).get("pct", 0)
        st.progress(last_pct / 100)
        st.caption(f"Progress: {last_pct}%")

    if not compact and events:
        st.markdown("**Event log:**")
        for e in events:
            evt     = e.get("event_type", "?")
            payload = e.get("payload", {})
            seq     = e.get("seq", "?")
            pct     = payload.get("pct", "")
            label   = payload.get("label", "")
            phase   = payload.get("phase", "")
            node    = payload.get("node", "")
            color   = {"gate": "#fde68a", "node": "#93c5fd", "complete": "#86efac"}.get(evt, "#c0c0e0")
            parts   = (
                (f" ({pct}%)" if pct != "" else "") +
                (f" — {label}"  if label else "") +
                (f" [{phase}]"  if phase else "") +
                (f" @ {node}"   if node  else "")
            )
            st.markdown(
                f'<div class="vf-ac-item">'
                f'<span class="vf-ac-id">#{seq}</span>'
                f'<span style="color:#7878aa;">[{evt}]</span>'
                f'<span style="color:{color};">{parts}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # Gate summaries
        gate_evts = [e for e in events if e.get("event_type") == "gate"]
        if gate_evts:
            st.markdown("<br>**Gate Summaries:**")
            for ge in gate_evts:
                gp     = ge.get("payload", {})
                gname  = gp.get("gate", gp.get("label", "Gate"))
                gst    = gp.get("status", "?")
                gscore = gp.get("score", "")
                gicon  = "✅" if gst == "pass" else "❌"
                st.markdown(f"{gicon} **{gname}**" + (f" — score: {gscore}" if gscore else ""))

    if status == "running":
        time.sleep(3)
        st.rerun()
    elif status == "completed" and not compact:
        st.balloons()
        st.success("🎉 Generation complete!")
        proj_id = st.session_state.get("active_proj_id")
        if proj_id:
            bundle_url = f"{API_BASE}/api/v1/projects/{proj_id}/bundle"
            st.markdown(
                f'<a href="{bundle_url}" target="_blank">'
                f'<button style="background:linear-gradient(135deg,#16a34a,#15803d);'
                f'color:white;border:none;border-radius:8px;padding:0.5rem 1.5rem;'
                f'font-weight:600;cursor:pointer;">📦 Download Bundle</button></a>',
                unsafe_allow_html=True,
            )
    elif status == "failed" and not compact:
        st.error("❌ Generation failed. Check logs for details.")


def page_job_console() -> None:
    st.markdown('<div class="vf-section-header">⚡ Job Console</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="vf-section-sub">Live node phase iteration, gate summaries, and bundle download.</div>',
        unsafe_allow_html=True,
    )

    active_job_id = st.session_state.get("active_job_id", "")
    job_id_input  = st.text_input(
        "Job ID", value=active_job_id, placeholder="paste job UUID…", key="console_job_id"
    )
    if job_id_input:
        st.session_state["active_job_id"] = job_id_input

    if not job_id_input:
        st.markdown("#### Recent Jobs")
        proj_id = st.session_state.get("active_proj_id")
        if proj_id:
            jobs_resp = api_get(f"/api/v1/projects/{proj_id}/jobs") or []
            jobs = jobs_resp if isinstance(jobs_resp, list) else jobs_resp.get("items", [])
            for j in (jobs[:10] if jobs else []):
                jid      = j.get("id", j.get("job_id", "?"))
                jst      = j.get("status", "?")
                jicon    = STATUS_ICONS.get(jst, "⚪")
                jcreated = j.get("created_at", "")[:19].replace("T", " ")
                if st.button(f"{jicon} {jid[:8]}… — `{jst}` — {jcreated}", key=f"job_btn_{jid}"):
                    st.session_state["active_job_id"] = jid
                    st.rerun()
            if not jobs:
                st.info("No jobs found for this project.")
        else:
            st.info("Run Lock & Generate from Review Workspace first.")
        return

    st.divider()
    col_r, col_c, _col = st.columns([1, 1, 4])
    with col_r:
        if st.button("🔄 Refresh", key="refresh_btn"):
            st.rerun()
    with col_c:
        if st.button("✕ Clear", key="clear_job_btn"):
            st.session_state.pop("active_job_id", None)
            st.rerun()

    _render_job_progress(job_id_input, compact=False)

    # Node phase table
    events = api_get(f"/api/v1/jobs/{job_id_input}/events") or []
    node_evts = [e for e in events if e.get("event_type") in ("node_start", "node_complete", "node")]
    if node_evts:
        st.markdown("#### Node Phase Iterations")
        rows_html = "".join(
            f'<div class="vf-field-row">'
            f'<span class="vf-field-key">{"✅" if ne.get("payload",{}).get("success",True) else "❌"} '
            f'{ne.get("payload",{}).get("node", ne.get("event_type","?"))}</span>'
            f'<span class="vf-field-value">'
            f'{ne.get("payload",{}).get("phase","")} '
            f'{"(" + str(ne.get("payload",{}).get("duration_ms","")) + "ms)" if ne.get("payload",{}).get("duration_ms") else ""}'
            f'</span></div>'
            for ne in node_evts
        )
        st.markdown(f'<div class="vf-card">{rows_html}</div>', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# Sidebar + router
# ══════════════════════════════════════════════════════════════════════════════

# Guard: show login page if not authenticated
if not _is_logged_in():
    page_login()
    st.stop()

with st.sidebar:
    st.markdown("""
    <div style="padding:1rem 0 1.5rem;text-align:center;">
        <div style="font-size:2.5rem;">🔥</div>
        <div style="font-size:1.2rem;font-weight:700;color:#c4b5fd;">VibeForge</div>
        <div style="font-size:0.72rem;color:#7878aa;margin-top:4px;">Spec → Code Platform</div>
    </div>
    """, unsafe_allow_html=True)

    auth_mode  = st.session_state.get("auth_mode", "dev")
    user_email = st.session_state.get("user_email", "dev@vibeforge.dev")
    mode_badge = "🔑 Keycloak" if auth_mode == "keycloak" else "⚡ Dev Mode"
    st.markdown(
        f'<div style="font-size:0.75rem;color:#7878aa;text-align:center;margin-bottom:1rem;">'
        f'{mode_badge}<br><span style="color:#a0a0c0;">{user_email}</span></div>',
        unsafe_allow_html=True,
    )

    page = st.radio(
        "Navigate",
        ["🧬 Spec Sheet", "🔍 Review Workspace", "⚡ Job Console"],
        key="nav",
        label_visibility="collapsed",
    )

    st.divider()

    # System health widget
    try:
        health = httpx.get(f"{API_BASE}/health", timeout=3.0).json()
        api_ok = health.get("status") == "healthy"
        pg_ok  = health.get("checks", {}).get("postgres") == "ok"
        rd_ok  = health.get("checks", {}).get("redis")    == "ok"
    except Exception:
        api_ok = pg_ok = rd_ok = False

    st.markdown("**System Status**")
    st.markdown(
        f"{'🟢' if api_ok else '🔴'} API &nbsp;&nbsp;"
        f"{'🟢' if pg_ok else '🔴'} Postgres &nbsp;&nbsp;"
        f"{'🟢' if rd_ok else '🔴'} Redis",
        unsafe_allow_html=True,
    )

    st.divider()
    st.caption(f"API: `{API_BASE}`")
    st.caption(f"KC:  `{KC_BASE}`")

    if st.button("🚪 Logout", key="logout_btn"):
        for k in ["token", "token_time", "auth_mode", "user_email", "last_spec",
                  "last_proj_id", "active_job_id", "amendments",
                  "accepted_amendments", "rejected_amendments"]:
            st.session_state.pop(k, None)
        st.rerun()


# ── Page router ──────────────────────────────────────────────────────────────
if page == "🧬 Spec Sheet":
    page_spec_sheet()
elif page == "🔍 Review Workspace":
    page_review_workspace()
else:
    page_job_console()
