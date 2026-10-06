"""
domain_packs/v0/compiler.py — Deterministic Spec Compiler

Takes a dict of user option selections and produces a fully-populated AppSpec.
Every mapping is deterministic — the same selections ALWAYS produce the same
AppSpec and therefore the same canonical_hash.

This is what makes P3 (semantic caching) possible: before enqueueing a generation
job, the API computes canonical_hash and checks if it already exists for this
tenant. If yes → return existing job_id. If no → freeze new spec + enqueue.

Rules:
  - No randomness, no timestamps, no external calls
  - Pure function: compile_spec(selections) → AppSpec
  - Adding new options must never change the hash of old selections
"""
from __future__ import annotations

from models.spec_ir import (
    AcceptanceCriterion,
    ApiModel,
    DomainModel,
    Endpoint,
    Entity,
    EntityField,
    IntegrationSpec,
    NonFunctionalReqs,
    SecuritySpec,
    UiModel,
    WorkflowSpec,
    WorkflowStep,
    AppSpec,
)
from domain_packs.v0.options import OPTION_GRAPH, validate_selections


# =============================================================================
# Compiler
# =============================================================================

def compile_spec(selections: dict[str, str], app_name: str = "", description: str = "", vertical: str = "generic") -> AppSpec:
    """
    Compiles a complete AppSpec from a validated set of option selections.

    Args:
        selections: dict of {option_key: chosen_value}. Missing keys get defaults.
        app_name:   Optional human-readable app name (stored in spec, affects hash).
        description: Optional app description.

    Returns:
        A fully-populated AppSpec with a stable canonical_hash.
    """
    sel = validate_selections(selections)

    return AppSpec(
        schema_version="1.0",
        app_name=app_name,
        description=description,
        vertical=vertical,

        domain_model=_build_domain_model(sel),
        api_model=_build_api_model(sel),
        ui_model=_build_ui_model(sel),
        workflow=_build_workflow(sel),
        integration=_build_integration(sel),
        security=_build_security(sel),
        nfr=_build_nfr(sel),
        acceptance=_build_acceptance(sel),
    )


# =============================================================================
# Section builders — each maps relevant option keys to their sub-model
# =============================================================================

def _build_domain_model(sel: dict) -> DomainModel:
    """
    Phase 0: stub domain with a generic User entity.
    Phase 1+: populated from entity wizard selections.
    """
    user_fields = [
        EntityField(name="id",         type="uuid",     required=True, unique=True),
        EntityField(name="email",       type="string",   required=True, unique=True),
        EntityField(name="created_at",  type="datetime", required=True),
        EntityField(name="updated_at",  type="datetime", required=True),
    ]

    if sel["auth_strategy"] == "api_key":
        user_fields.append(
            EntityField(name="api_key", type="string", required=True, unique=True)
        )

    return DomainModel(
        entities=[Entity(name="User", fields=user_fields, timestamps=True)],
    )


def _build_api_model(sel: dict) -> ApiModel:
    auth = sel["auth_strategy"]
    # Always generate a baseline CRUD surface + auth endpoints.
    # The completeness validator requires len(endpoints) >= 1.
    endpoints = [
        Endpoint(method="POST",   path="/auth/token",           description="Obtain auth token",     auth_required=False),
        Endpoint(method="POST",   path="/api/v1/users",          description="Create user",           auth_required=True,  roles=["admin"]),
        Endpoint(method="GET",    path="/api/v1/users",          description="List users",            auth_required=True,  roles=["admin"]),
        Endpoint(method="GET",    path="/api/v1/users/{id}",     description="Get user by ID",        auth_required=True),
        Endpoint(method="PATCH",  path="/api/v1/users/{id}",     description="Update user",           auth_required=True),
        Endpoint(method="DELETE", path="/api/v1/users/{id}",     description="Delete user",           auth_required=True,  roles=["admin"]),
        Endpoint(method="GET",    path="/health",                description="Liveness check",        auth_required=False),
    ]
    if sel.get("async_jobs") == "yes":
        endpoints += [
            Endpoint(method="POST", path="/api/v1/jobs",         description="Enqueue background job", auth_required=True),
            Endpoint(method="GET",  path="/api/v1/jobs/{id}",    description="Get job status",         auth_required=True),
        ]
    if sel.get("search_enabled") == "yes":
        endpoints.append(
            Endpoint(method="GET", path="/api/v1/search",        description="Full-text search",      auth_required=True)
        )
    return ApiModel(
        style=sel["api_style"],
        version_prefix="/api/v1",
        auth_strategy=auth,
        rate_limiting=True,
        endpoints=endpoints,
    )


def _build_ui_model(sel: dict) -> UiModel:
    framework = sel["ui_framework"]

    # nextjs uses its own styling conventions
    styling = "tailwind"
    state  = "zustand"

    if framework == "vue":
        styling = "css_modules"
        state   = "pinia"
    elif framework == "svelte":
        styling = "css_modules"
        state   = "none"
    elif framework == "none":
        styling = "none"
        state   = "none"

    return UiModel(
        framework=framework,
        styling=styling,
        state_management=state,
    )


def _build_workflow(sel: dict) -> WorkflowSpec:
    async_enabled = sel["async_jobs"] == "yes"
    steps = []
    if async_enabled:
        steps.append(WorkflowStep(
            name="background_job",
            type="async_job",
            description="Generic background job processor",
            timeout_seconds=300,
        ))
    return WorkflowSpec(
        async_jobs=async_enabled,
        notifications=async_enabled,   # notifications require async infra
        steps=steps,
    )


def _build_integration(sel: dict) -> IntegrationSpec:
    return IntegrationSpec(
        integrations=[],
        webhooks_enabled=sel["async_jobs"] == "yes",
    )


def _build_security(sel: dict) -> SecuritySpec:
    compliance = sel["compliance_tier"]
    pii: list[str] = []

    if compliance in ("gdpr", "hipaa"):
        pii = ["email", "name", "date_of_birth", "address"]
    if compliance == "hipaa":
        pii += ["diagnosis", "medication", "health_record_id"]

    return SecuritySpec(
        auth_strategy=sel["auth_strategy"],
        rbac_enabled=True,
        roles=["admin", "user"],
        mfa_required=compliance in ("hipaa", "pci"),
        data_classification=(
            "restricted" if compliance in ("hipaa", "pci")
            else "confidential" if compliance == "gdpr"
            else "internal"
        ),
        pii_fields=pii,
    )


def _build_nfr(sel: dict) -> NonFunctionalReqs:
    return NonFunctionalReqs(
        db_tier=sel["db_tier"],
        deploy_target=sel["deploy_target"],
        cache_strategy="redis" if sel["async_jobs"] == "yes" else "none",
        search_enabled=sel["search_enabled"] == "yes",
    )


def _build_acceptance(sel: dict) -> list[AcceptanceCriterion]:
    """
    Generates verifiable acceptance criteria from selections.
    The QA Gate checks these automatically in Phase 2+.
    """
    criteria: list[AcceptanceCriterion] = [
        AcceptanceCriterion(
            id="AC-001",
            description=f"API responds to /health with 200 OK",
            verifiable=True,
            test_hint="GET /health → assert status_code == 200",
        ),
        AcceptanceCriterion(
            id="AC-002",
            description=f"Authentication via {sel['auth_strategy']} works correctly",
            verifiable=True,
            test_hint=f"POST /auth/token with valid credentials → 200 with token",
        ),
        AcceptanceCriterion(
            id="AC-003",
            description=f"CRUD operations on User entity succeed",
            verifiable=True,
            test_hint="POST/GET/PATCH/DELETE /api/v1/users → expected status codes",
        ),
    ]

    if sel["async_jobs"] == "yes":
        criteria.append(AcceptanceCriterion(
            id="AC-004",
            description="Background jobs are processed and status is queryable",
            verifiable=True,
            test_hint="POST /jobs → 202, poll GET /jobs/{id} until completed",
        ))

    if sel["search_enabled"] == "yes":
        criteria.append(AcceptanceCriterion(
            id="AC-005",
            description="Full-text search returns relevant results",
            verifiable=True,
            test_hint="GET /api/v1/search?q=test → non-empty results array",
        ))

    if sel["compliance_tier"] in ("gdpr", "hipaa", "pci"):
        criteria.append(AcceptanceCriterion(
            id="AC-006",
            description=f"PII fields are masked in logs ({sel['compliance_tier'].upper()} compliance)",
            verifiable=True,
            test_hint="Search application logs — email/name fields must not appear in plaintext",
        ))

    return criteria
