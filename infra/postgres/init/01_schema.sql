-- =============================================================================
-- 01_schema.sql — VibeForge main database schema
--
-- Runs on first Postgres boot via docker-entrypoint-initdb.d/.
-- Every table:
--   • carries tenant_id NOT NULL (except tenants itself) — Contract C9
--   • has a covering index on (tenant_id, ...)
--   • uses UUID primary keys with gen_random_uuid()
--
-- Append-only enforcement for job_events and audit_log is applied in
-- 02_rls.sql via dedicated INSERT-only policies and role grants.
-- =============================================================================

-- Enable pgcrypto for gen_random_uuid() (also available as built-in in PG 13+)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- =============================================================================
-- Application role used by the API and Worker processes.
-- This role is NOT a superuser and is NOT the table owner, so RLS applies.
-- =============================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vibeforge_app') THEN
        CREATE ROLE vibeforge_app NOLOGIN;
    END IF;
END
$$;

-- Grant the app role to the DB owner so it can operate as vibeforge_app
-- (in practice, the connection user is granted this role at connection time)
GRANT vibeforge_app TO vibeforge;

-- =============================================================================
-- TENANTS
-- The root entity. No tenant_id FK on this table (it is the anchor).
-- =============================================================================
CREATE TABLE IF NOT EXISTS tenants (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT        NOT NULL,
    slug        TEXT        NOT NULL UNIQUE,
    tier        TEXT        NOT NULL DEFAULT 'starter'
                            CHECK (tier IN ('starter', 'growth', 'enterprise')),
    config      JSONB       NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tenants_slug ON tenants (slug);

-- =============================================================================
-- USERS
-- Maps a Keycloak subject (sub claim) to a tenant-scoped application user.
-- =============================================================================
CREATE TABLE IF NOT EXISTS users (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    keycloak_sub    TEXT        NOT NULL UNIQUE,        -- OIDC sub claim
    email           TEXT        NOT NULL,
    role            TEXT        NOT NULL DEFAULT 'developer'
                                CHECK (role IN ('admin', 'developer', 'viewer')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_tenant_id         ON users (tenant_id);
CREATE INDEX IF NOT EXISTS idx_users_keycloak_sub      ON users (keycloak_sub);
CREATE INDEX IF NOT EXISTS idx_users_tenant_email      ON users (tenant_id, email);

-- =============================================================================
-- PROJECTS
-- A named workspace that groups related application specs.
-- =============================================================================
CREATE TABLE IF NOT EXISTS projects (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    owner_id    UUID        NOT NULL REFERENCES users(id),
    name        TEXT        NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_projects_tenant_id     ON projects (tenant_id);
CREATE INDEX IF NOT EXISTS idx_projects_owner_id      ON projects (tenant_id, owner_id);
CREATE INDEX IF NOT EXISTS idx_projects_created_at    ON projects (tenant_id, created_at DESC);

-- =============================================================================
-- APPLICATION_SPECS
-- Immutable once frozen (frozen_at IS NOT NULL).
-- Edits create a new version (v+1); jobs reference exactly one frozen version.
-- canonical_hash = SHA-256 of normalised spec_data JSON (computed by app).
-- =============================================================================
CREATE TABLE IF NOT EXISTS application_specs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    project_id      UUID        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version         INTEGER     NOT NULL DEFAULT 1 CHECK (version >= 1),
    spec_data       JSONB       NOT NULL DEFAULT '{}',
    canonical_hash  TEXT,                                    -- set on freeze
    frozen_at       TIMESTAMPTZ,                             -- NULL = draft
    created_by      UUID        NOT NULL REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (project_id, version)
);

CREATE INDEX IF NOT EXISTS idx_specs_tenant_id         ON application_specs (tenant_id);
CREATE INDEX IF NOT EXISTS idx_specs_project_id        ON application_specs (tenant_id, project_id);
CREATE INDEX IF NOT EXISTS idx_specs_canonical_hash    ON application_specs (canonical_hash)
    WHERE canonical_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_specs_frozen            ON application_specs (tenant_id, frozen_at)
    WHERE frozen_at IS NOT NULL;

-- =============================================================================
-- SPEC_PROVENANCE
-- Audit trail: which option-graph click produced which JSON patch on which spec.
-- Append-only semantics enforced in 02_rls.sql.
-- =============================================================================
CREATE TABLE IF NOT EXISTS spec_provenance (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    spec_id     UUID        NOT NULL REFERENCES application_specs(id) ON DELETE CASCADE,
    option_id   TEXT        NOT NULL,       -- the checkbox/preset ID from the domain pack
    json_path   TEXT        NOT NULL,       -- JSONPath target in spec_data
    patch_data  JSONB       NOT NULL,       -- the deterministic JSON Patch delta
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_provenance_tenant_id    ON spec_provenance (tenant_id);
CREATE INDEX IF NOT EXISTS idx_provenance_spec_id      ON spec_provenance (tenant_id, spec_id);
CREATE INDEX IF NOT EXISTS idx_provenance_applied_at   ON spec_provenance (tenant_id, applied_at DESC);

-- =============================================================================
-- JOBS
-- Represents one generation (or other async) run against a frozen spec.
-- idempotency_key (UNIQUE) prevents double-submit. Job truth lives here,
-- not in Redis — Redis is never the system of record (P5).
-- =============================================================================
CREATE TABLE IF NOT EXISTS jobs (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    spec_id          UUID        NOT NULL REFERENCES application_specs(id),
    status           TEXT        NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending', 'queued', 'running',
                                                   'completed', 'failed', 'cancelled')),
    job_type         TEXT        NOT NULL DEFAULT 'generation'
                                 CHECK (job_type IN ('generation', 'review', 'sandbox', 'cache_warm')),
    priority         INTEGER     NOT NULL DEFAULT 5 CHECK (priority BETWEEN 1 AND 10),
    idempotency_key  TEXT        NOT NULL UNIQUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    result_ref       TEXT,                   -- MinIO object path to result bundle
    error_detail     JSONB,                  -- structured error info on failure
    arq_job_id       TEXT                    -- Arq's internal job ID for correlation
);

CREATE INDEX IF NOT EXISTS idx_jobs_tenant_id          ON jobs (tenant_id);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_status      ON jobs (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_created     ON jobs (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_spec_id            ON jobs (tenant_id, spec_id);
CREATE INDEX IF NOT EXISTS idx_jobs_idempotency        ON jobs (idempotency_key);  -- already UNIQUE, explicit for clarity

-- =============================================================================
-- JOB_EVENTS
-- Append-only event stream for a job — drives SSE and the job console.
-- No UPDATE or DELETE permitted (enforced via RLS in 02_rls.sql).
-- =============================================================================
CREATE TABLE IF NOT EXISTS job_events (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    job_id      UUID        NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    event_type  TEXT        NOT NULL,   -- e.g. 'progress', 'phase_start', 'error', 'complete'
    payload     JSONB       NOT NULL DEFAULT '{}',
    seq         BIGSERIAL   NOT NULL,   -- monotonic sequence for ordered SSE replay
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_job_events_job_id        ON job_events (tenant_id, job_id, seq ASC);
CREATE INDEX IF NOT EXISTS idx_job_events_created_at    ON job_events (tenant_id, created_at DESC);

-- =============================================================================
-- GATE_REPORTS
-- Machine-readable output of the QA Gate for each fix iteration.
-- The Reviewer agent reads this — real facts, not LLM opinions.
-- =============================================================================
CREATE TABLE IF NOT EXISTS gate_reports (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    job_id      UUID        NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    iteration   INTEGER     NOT NULL DEFAULT 1 CHECK (iteration >= 1),
    passed      BOOLEAN     NOT NULL,
    report_data JSONB       NOT NULL DEFAULT '{}',  -- compile, test, scan results
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (job_id, iteration)
);

CREATE INDEX IF NOT EXISTS idx_gate_reports_tenant_id   ON gate_reports (tenant_id);
CREATE INDEX IF NOT EXISTS idx_gate_reports_job_id      ON gate_reports (tenant_id, job_id);

-- =============================================================================
-- LLM_CALLS
-- Cost ledger: every model invocation is recorded for budget tracking and
-- the governance dashboard. Written by LiteLLM callback.
-- =============================================================================
CREATE TABLE IF NOT EXISTS llm_calls (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    job_id              UUID        REFERENCES jobs(id) ON DELETE SET NULL,
    model_alias         TEXT        NOT NULL,
    prompt_tokens       INTEGER     NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens   INTEGER     NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    cost_usd            NUMERIC(12,8) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    latency_ms          INTEGER,
    gate_approved       BOOLEAN,    -- was this call approved by the Metacognition Gate?
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_tenant_id      ON llm_calls (tenant_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_job_id         ON llm_calls (tenant_id, job_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_model_alias    ON llm_calls (tenant_id, model_alias);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created_at     ON llm_calls (tenant_id, created_at DESC);

-- =============================================================================
-- BUDGETS
-- Per-tenant spend tracking. One row per tenant per billing period.
-- =============================================================================
CREATE TABLE IF NOT EXISTS budgets (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    budget_usd      NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (budget_usd >= 0),
    spent_usd       NUMERIC(12,8) NOT NULL DEFAULT 0 CHECK (spent_usd >= 0),
    period_start    DATE        NOT NULL,
    period_end      DATE        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CHECK (period_end > period_start),
    UNIQUE (tenant_id, period_start)
);

CREATE INDEX IF NOT EXISTS idx_budgets_tenant_id        ON budgets (tenant_id);
CREATE INDEX IF NOT EXISTS idx_budgets_period           ON budgets (tenant_id, period_start, period_end);

-- =============================================================================
-- AUDIT_LOG
-- Immutable append-only record of every significant action in the system.
-- No UPDATE or DELETE permitted (enforced via RLS in 02_rls.sql).
-- =============================================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    actor_id        UUID        REFERENCES users(id) ON DELETE SET NULL,
    action          TEXT        NOT NULL,        -- e.g. 'spec.freeze', 'job.enqueue', 'escalation.trigger'
    resource_type   TEXT        NOT NULL,        -- e.g. 'spec', 'job', 'project'
    resource_id     UUID,
    diff_data       JSONB,                       -- before/after snapshot for state changes
    ip_address      INET,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_id      ON audit_log (tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor_id       ON audit_log (tenant_id, actor_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_resource       ON audit_log (tenant_id, resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at     ON audit_log (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_action         ON audit_log (tenant_id, action);

-- =============================================================================
-- ESCALATION_REGISTER
-- Tracks every escalation to a commercial model (D2): triggers, cost, outcome.
-- Also tracks "avoided via memory" outcomes from Escalation Memory (8.3).
-- =============================================================================
CREATE TABLE IF NOT EXISTS escalation_register (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    job_id              UUID        NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    trigger_reason      TEXT        NOT NULL,    -- '3_failed_iterations' | 'complexity_score' | 'tenant_preference'
    avoided_via_memory  BOOLEAN     NOT NULL DEFAULT FALSE,
    memory_hit_id       UUID,                    -- FK to escalation_memory if avoided
    commercial_model    TEXT,                    -- null if avoided
    prompt_tokens       INTEGER,
    cost_usd            NUMERIC(12,8),
    outcome             TEXT        CHECK (outcome IN ('success', 'failure', 'timeout')),
    pii_scan_clean      BOOLEAN,                 -- was pre-egress PII scan clean?
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_escalation_tenant_id     ON escalation_register (tenant_id);
CREATE INDEX IF NOT EXISTS idx_escalation_job_id        ON escalation_register (tenant_id, job_id);
CREATE INDEX IF NOT EXISTS idx_escalation_avoided       ON escalation_register (tenant_id, avoided_via_memory);

-- =============================================================================
-- ESCALATION_MEMORY
-- Verified commercial answers turned into local knowledge (8.3).
-- Written only after a gate pass. Checked before the escalation gate deliberates.
-- =============================================================================
CREATE TABLE IF NOT EXISTS escalation_memory (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    problem_signature   TEXT        NOT NULL,    -- error_class + stack_profile + rule hash
    stack_profile       TEXT        NOT NULL,
    error_class         TEXT        NOT NULL,
    fix_summary         TEXT        NOT NULL,    -- human-readable summary
    fix_data            JSONB       NOT NULL,    -- structured fix (file patches, etc.)
    source_job_id       UUID        REFERENCES jobs(id) ON DELETE SET NULL,
    scope               TEXT        NOT NULL DEFAULT 'tenant'
                                    CHECK (scope IN ('tenant', 'global')),
    promoted_at         TIMESTAMPTZ,             -- when promoted to global by human review
    promoted_by         UUID        REFERENCES users(id),
    gate_verified       BOOLEAN     NOT NULL DEFAULT FALSE,
    use_count           INTEGER     NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (tenant_id, problem_signature)
);

CREATE INDEX IF NOT EXISTS idx_esc_memory_tenant_id     ON escalation_memory (tenant_id);
CREATE INDEX IF NOT EXISTS idx_esc_memory_signature     ON escalation_memory (tenant_id, problem_signature);
CREATE INDEX IF NOT EXISTS idx_esc_memory_global        ON escalation_memory (scope) WHERE scope = 'global';

-- =============================================================================
-- GATE_DECISIONS (Metacognition Gate — Section 6)
-- Every gate decision (NEEDED / NOT_NEEDED / NARROW_QUERY) is logged here.
-- The gate_decisions table becomes the Tier 2 training set.
-- =============================================================================
CREATE TABLE IF NOT EXISTS gate_decisions (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    job_id          UUID        REFERENCES jobs(id) ON DELETE SET NULL,
    decision_point  TEXT        NOT NULL    -- 'retrieval' | 'tool_call' | 'escalation'
                                CHECK (decision_point IN ('retrieval', 'tool_call', 'escalation')),
    tier            INTEGER     NOT NULL CHECK (tier IN (0, 1, 2)),
    outcome         TEXT        NOT NULL
                                CHECK (outcome IN ('NEEDED', 'NOT_NEEDED', 'NARROW_QUERY')),
    rationale       TEXT,
    context_hash    TEXT,       -- hash of the input context for dedup / training
    latency_ms      INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gate_decisions_tenant_id ON gate_decisions (tenant_id);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_job_id    ON gate_decisions (tenant_id, job_id);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_point     ON gate_decisions (tenant_id, decision_point);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_tier      ON gate_decisions (tier, outcome);

-- =============================================================================
-- GRANTS — give vibeforge_app DML access to all tables
-- SELECT, INSERT, UPDATE on regular tables
-- Only SELECT + INSERT on append-only tables (job_events, audit_log, spec_provenance)
-- =============================================================================
GRANT SELECT, INSERT, UPDATE ON
    tenants, users, projects, application_specs, jobs,
    gate_reports, llm_calls, budgets, escalation_register,
    escalation_memory, gate_decisions
TO vibeforge_app;

GRANT SELECT, INSERT ON
    job_events, audit_log, spec_provenance
TO vibeforge_app;

-- Grant sequence usage
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO vibeforge_app;

-- =============================================================================
-- Seed data — dev tenant and admin user (local dev only)
-- Idempotent: INSERT ... ON CONFLICT DO NOTHING
-- =============================================================================
INSERT INTO tenants (id, name, slug, tier)
VALUES ('00000000-0000-0000-0000-000000000001', 'Dev Tenant', 'dev-tenant', 'starter')
ON CONFLICT (id) DO NOTHING;

INSERT INTO budgets (tenant_id, budget_usd, spent_usd, period_start, period_end)
VALUES (
    '00000000-0000-0000-0000-000000000001',
    100.00, 0,
    date_trunc('month', CURRENT_DATE)::date,
    (date_trunc('month', CURRENT_DATE) + interval '1 month - 1 day')::date
)
ON CONFLICT (tenant_id, period_start) DO NOTHING;
