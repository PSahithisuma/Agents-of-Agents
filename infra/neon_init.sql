-- VibeForge — Neon.tech Database Setup
-- Run this entire file in the Neon SQL Editor after creating the database
-- URL: https://console.neon.tech -> Your Project -> SQL Editor

-- Step 1: Extensions
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Step 2: App role (Neon uses neon_superuser as owner — we adapt)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vibeforge_app') THEN
        CREATE ROLE vibeforge_app NOLOGIN;
    END IF;
END
$$;

-- Step 3: All tables (from 01_schema.sql)
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

CREATE TABLE IF NOT EXISTS users (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    keycloak_sub    TEXT        NOT NULL UNIQUE,
    email           TEXT        NOT NULL,
    role            TEXT        NOT NULL DEFAULT 'developer'
                                CHECK (role IN ('admin', 'developer', 'viewer')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_users_tenant_id         ON users (tenant_id);
CREATE INDEX IF NOT EXISTS idx_users_keycloak_sub      ON users (keycloak_sub);
CREATE INDEX IF NOT EXISTS idx_users_tenant_email      ON users (tenant_id, email);

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

CREATE TABLE IF NOT EXISTS application_specs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    project_id      UUID        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version         INTEGER     NOT NULL DEFAULT 1 CHECK (version >= 1),
    spec_data       JSONB       NOT NULL DEFAULT '{}',
    canonical_hash  TEXT,
    frozen_at       TIMESTAMPTZ,
    created_by      UUID        NOT NULL REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, version)
);
CREATE INDEX IF NOT EXISTS idx_specs_tenant_id         ON application_specs (tenant_id);
CREATE INDEX IF NOT EXISTS idx_specs_project_id        ON application_specs (tenant_id, project_id);
CREATE INDEX IF NOT EXISTS idx_specs_canonical_hash    ON application_specs (canonical_hash) WHERE canonical_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_specs_frozen            ON application_specs (tenant_id, frozen_at) WHERE frozen_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS spec_provenance (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    spec_id     UUID        NOT NULL REFERENCES application_specs(id) ON DELETE CASCADE,
    option_id   TEXT        NOT NULL,
    json_path   TEXT        NOT NULL,
    patch_data  JSONB       NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_provenance_tenant_id    ON spec_provenance (tenant_id);
CREATE INDEX IF NOT EXISTS idx_provenance_spec_id      ON spec_provenance (tenant_id, spec_id);

CREATE TABLE IF NOT EXISTS jobs (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    spec_id          UUID        NOT NULL REFERENCES application_specs(id),
    status           TEXT        NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending','queued','running','completed','failed','cancelled')),
    job_type         TEXT        NOT NULL DEFAULT 'generation'
                                 CHECK (job_type IN ('generation','review','sandbox','cache_warm')),
    priority         INTEGER     NOT NULL DEFAULT 5 CHECK (priority BETWEEN 1 AND 10),
    idempotency_key  TEXT        NOT NULL UNIQUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    result_ref       TEXT,
    error_detail     JSONB,
    arq_job_id       TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_id          ON jobs (tenant_id);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_status      ON jobs (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_created     ON jobs (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_spec_id            ON jobs (tenant_id, spec_id);
CREATE INDEX IF NOT EXISTS idx_jobs_idempotency        ON jobs (idempotency_key);

CREATE TABLE IF NOT EXISTS job_events (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    job_id      UUID        NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    event_type  TEXT        NOT NULL,
    payload     JSONB       NOT NULL DEFAULT '{}',
    seq         BIGSERIAL   NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_job_events_job_id        ON job_events (tenant_id, job_id, seq ASC);

CREATE TABLE IF NOT EXISTS gate_reports (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    job_id      UUID        NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    iteration   INTEGER     NOT NULL DEFAULT 1 CHECK (iteration >= 1),
    passed      BOOLEAN     NOT NULL,
    report_data JSONB       NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (job_id, iteration)
);
CREATE INDEX IF NOT EXISTS idx_gate_reports_tenant_id   ON gate_reports (tenant_id);
CREATE INDEX IF NOT EXISTS idx_gate_reports_job_id      ON gate_reports (tenant_id, job_id);

CREATE TABLE IF NOT EXISTS llm_calls (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    job_id              UUID        REFERENCES jobs(id) ON DELETE SET NULL,
    model_alias         TEXT        NOT NULL,
    prompt_tokens       INTEGER     NOT NULL DEFAULT 0,
    completion_tokens   INTEGER     NOT NULL DEFAULT 0,
    cost_usd            NUMERIC(12,8) NOT NULL DEFAULT 0,
    latency_ms          INTEGER,
    gate_approved       BOOLEAN,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_tenant_id      ON llm_calls (tenant_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_job_id         ON llm_calls (tenant_id, job_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created_at     ON llm_calls (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS budgets (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    budget_usd      NUMERIC(12,2) NOT NULL DEFAULT 0,
    spent_usd       NUMERIC(12,8) NOT NULL DEFAULT 0,
    period_start    DATE        NOT NULL,
    period_end      DATE        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (period_end > period_start),
    UNIQUE (tenant_id, period_start)
);
CREATE INDEX IF NOT EXISTS idx_budgets_tenant_id        ON budgets (tenant_id);
CREATE INDEX IF NOT EXISTS idx_budgets_period           ON budgets (tenant_id, period_start, period_end);

CREATE TABLE IF NOT EXISTS audit_log (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    actor_id        UUID        REFERENCES users(id) ON DELETE SET NULL,
    action          TEXT        NOT NULL,
    resource_type   TEXT        NOT NULL,
    resource_id     UUID,
    diff_data       JSONB,
    ip_address      INET,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_id      ON audit_log (tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at     ON audit_log (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS escalation_register (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    job_id              UUID        NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    trigger_reason      TEXT        NOT NULL,
    avoided_via_memory  BOOLEAN     NOT NULL DEFAULT FALSE,
    memory_hit_id       UUID,
    commercial_model    TEXT,
    prompt_tokens       INTEGER,
    cost_usd            NUMERIC(12,8),
    outcome             TEXT        CHECK (outcome IN ('success','failure','timeout')),
    pii_scan_clean      BOOLEAN,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_escalation_tenant_id     ON escalation_register (tenant_id);
CREATE INDEX IF NOT EXISTS idx_escalation_job_id        ON escalation_register (tenant_id, job_id);

CREATE TABLE IF NOT EXISTS escalation_memory (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    problem_signature   TEXT        NOT NULL,
    stack_profile       TEXT        NOT NULL,
    error_class         TEXT        NOT NULL,
    fix_summary         TEXT        NOT NULL,
    fix_data            JSONB       NOT NULL,
    source_job_id       UUID        REFERENCES jobs(id) ON DELETE SET NULL,
    scope               TEXT        NOT NULL DEFAULT 'tenant' CHECK (scope IN ('tenant','global')),
    promoted_at         TIMESTAMPTZ,
    promoted_by         UUID        REFERENCES users(id),
    gate_verified       BOOLEAN     NOT NULL DEFAULT FALSE,
    use_count           INTEGER     NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, problem_signature)
);
CREATE INDEX IF NOT EXISTS idx_esc_memory_tenant_id     ON escalation_memory (tenant_id);
CREATE INDEX IF NOT EXISTS idx_esc_memory_signature     ON escalation_memory (tenant_id, problem_signature);

CREATE TABLE IF NOT EXISTS gate_decisions (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    job_id          UUID        REFERENCES jobs(id) ON DELETE SET NULL,
    decision_point  TEXT        NOT NULL CHECK (decision_point IN ('retrieval','tool_call','escalation')),
    tier            INTEGER     NOT NULL CHECK (tier IN (0,1,2)),
    outcome         TEXT        NOT NULL CHECK (outcome IN ('NEEDED','NOT_NEEDED','NARROW_QUERY')),
    rationale       TEXT,
    context_hash    TEXT,
    latency_ms      INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_tenant_id ON gate_decisions (tenant_id);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_job_id    ON gate_decisions (tenant_id, job_id);

-- Step 4: RLS function
CREATE OR REPLACE FUNCTION current_tenant_id()
RETURNS UUID LANGUAGE SQL STABLE AS $$
    SELECT NULLIF(current_setting('app.current_tenant_id', true), '')::uuid;
$$;

-- Step 5: Seed data
INSERT INTO tenants (id, name, slug, tier)
VALUES ('00000000-0000-0000-0000-000000000001', 'Dev Tenant', 'dev-tenant', 'starter')
ON CONFLICT (id) DO NOTHING;

INSERT INTO budgets (tenant_id, budget_usd, spent_usd, period_start, period_end)
VALUES (
    '00000000-0000-0000-0000-000000000001', 100.00, 0,
    date_trunc('month', CURRENT_DATE)::date,
    (date_trunc('month', CURRENT_DATE) + interval '1 month - 1 day')::date
) ON CONFLICT (tenant_id, period_start) DO NOTHING;

-- Done! All tables created. Verify with: SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';
