-- =============================================================================
-- 02_rls.sql — Row-Level Security policies (Contract C9)
--
-- Design:
--   • Every table (with tenant_id) gets FORCE ROW LEVEL SECURITY, meaning
--     the policy applies even to the table owner — not just non-owners.
--   • The app sets SET LOCAL app.current_tenant_id = '<uuid>' in every
--     transaction before any DML. This is the only way the policy is satisfied.
--   • A bypass role (vibeforge_superuser) is created for migrations and
--     administrative ops. It is NEVER used by the API or worker.
--   • Append-only tables (job_events, audit_log, spec_provenance) have NO
--     UPDATE or DELETE policies — those operations simply have no matching
--     policy and are rejected by the default-deny RLS framework.
-- =============================================================================

-- =============================================================================
-- Helper: a superuser bypass role for migrations / admin ops.
-- API and worker must NEVER connect as this role.
-- =============================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vibeforge_superuser') THEN
        CREATE ROLE vibeforge_superuser NOLOGIN BYPASSRLS;
    END IF;
END
$$;

GRANT vibeforge_superuser TO vibeforge;  -- the DB owner can escalate for migrations

-- =============================================================================
-- Utility function: safely read the current tenant ID from session config.
-- Returns NULL if not set (policy will evaluate to false → deny).
-- =============================================================================
CREATE OR REPLACE FUNCTION current_tenant_id()
RETURNS UUID
LANGUAGE SQL
STABLE
AS $$
    SELECT NULLIF(current_setting('app.current_tenant_id', true), '')::uuid;
$$;

-- =============================================================================
-- Macro: enable FORCE RLS on a table.
-- FORCE ensures the policy applies to the table owner too.
-- =============================================================================

-- tenants table: no tenant_id FK — accessed by matching id directly.
-- The API always queries by ID (derived from the JWT), so no policy needed
-- for standard app access. Superuser can read all; app role reads only its own.
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_self_select ON tenants
    FOR SELECT
    TO vibeforge_app
    USING (id = current_tenant_id());

-- tenants has no INSERT/UPDATE/DELETE via app role (managed by admin ops only)

-- =============================================================================
-- USERS
-- =============================================================================
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;

CREATE POLICY users_select ON users
    FOR SELECT TO vibeforge_app
    USING (tenant_id = current_tenant_id());

CREATE POLICY users_insert ON users
    FOR INSERT TO vibeforge_app
    WITH CHECK (tenant_id = current_tenant_id());

CREATE POLICY users_update ON users
    FOR UPDATE TO vibeforge_app
    USING (tenant_id = current_tenant_id())
    WITH CHECK (tenant_id = current_tenant_id());

-- =============================================================================
-- PROJECTS
-- =============================================================================
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects FORCE ROW LEVEL SECURITY;

CREATE POLICY projects_select ON projects
    FOR SELECT TO vibeforge_app
    USING (tenant_id = current_tenant_id());

CREATE POLICY projects_insert ON projects
    FOR INSERT TO vibeforge_app
    WITH CHECK (tenant_id = current_tenant_id());

CREATE POLICY projects_update ON projects
    FOR UPDATE TO vibeforge_app
    USING (tenant_id = current_tenant_id())
    WITH CHECK (tenant_id = current_tenant_id());

-- =============================================================================
-- APPLICATION_SPECS
-- Frozen specs are immutable: once frozen_at IS NOT NULL, no UPDATE is allowed.
-- Enforced here as an additional guard (the app also refuses to mutate a frozen spec).
-- =============================================================================
ALTER TABLE application_specs ENABLE ROW LEVEL SECURITY;
ALTER TABLE application_specs FORCE ROW LEVEL SECURITY;

CREATE POLICY specs_select ON application_specs
    FOR SELECT TO vibeforge_app
    USING (tenant_id = current_tenant_id());

CREATE POLICY specs_insert ON application_specs
    FOR INSERT TO vibeforge_app
    WITH CHECK (tenant_id = current_tenant_id());

-- UPDATE is only permitted on draft specs (frozen_at IS NULL)
CREATE POLICY specs_update ON application_specs
    FOR UPDATE TO vibeforge_app
    USING (tenant_id = current_tenant_id() AND frozen_at IS NULL)
    WITH CHECK (tenant_id = current_tenant_id() AND frozen_at IS NULL);

-- =============================================================================
-- SPEC_PROVENANCE — append-only
-- No UPDATE / DELETE policy → those operations are rejected by default-deny RLS.
-- =============================================================================
ALTER TABLE spec_provenance ENABLE ROW LEVEL SECURITY;
ALTER TABLE spec_provenance FORCE ROW LEVEL SECURITY;

CREATE POLICY spec_provenance_select ON spec_provenance
    FOR SELECT TO vibeforge_app
    USING (tenant_id = current_tenant_id());

CREATE POLICY spec_provenance_insert ON spec_provenance
    FOR INSERT TO vibeforge_app
    WITH CHECK (tenant_id = current_tenant_id());

-- No UPDATE / DELETE policy intentionally omitted → default-deny

-- =============================================================================
-- JOBS
-- =============================================================================
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs FORCE ROW LEVEL SECURITY;

CREATE POLICY jobs_select ON jobs
    FOR SELECT TO vibeforge_app
    USING (tenant_id = current_tenant_id());

CREATE POLICY jobs_insert ON jobs
    FOR INSERT TO vibeforge_app
    WITH CHECK (tenant_id = current_tenant_id());

CREATE POLICY jobs_update ON jobs
    FOR UPDATE TO vibeforge_app
    USING (tenant_id = current_tenant_id())
    WITH CHECK (tenant_id = current_tenant_id());

-- =============================================================================
-- JOB_EVENTS — append-only
-- =============================================================================
ALTER TABLE job_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_events FORCE ROW LEVEL SECURITY;

CREATE POLICY job_events_select ON job_events
    FOR SELECT TO vibeforge_app
    USING (tenant_id = current_tenant_id());

CREATE POLICY job_events_insert ON job_events
    FOR INSERT TO vibeforge_app
    WITH CHECK (tenant_id = current_tenant_id());

-- No UPDATE / DELETE policy intentionally omitted → default-deny

-- =============================================================================
-- GATE_REPORTS
-- =============================================================================
ALTER TABLE gate_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE gate_reports FORCE ROW LEVEL SECURITY;

CREATE POLICY gate_reports_select ON gate_reports
    FOR SELECT TO vibeforge_app
    USING (tenant_id = current_tenant_id());

CREATE POLICY gate_reports_insert ON gate_reports
    FOR INSERT TO vibeforge_app
    WITH CHECK (tenant_id = current_tenant_id());

-- Gate reports are immutable once written (no update policy)

-- =============================================================================
-- LLM_CALLS
-- =============================================================================
ALTER TABLE llm_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_calls FORCE ROW LEVEL SECURITY;

CREATE POLICY llm_calls_select ON llm_calls
    FOR SELECT TO vibeforge_app
    USING (tenant_id = current_tenant_id());

CREATE POLICY llm_calls_insert ON llm_calls
    FOR INSERT TO vibeforge_app
    WITH CHECK (tenant_id = current_tenant_id());

-- =============================================================================
-- BUDGETS
-- =============================================================================
ALTER TABLE budgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE budgets FORCE ROW LEVEL SECURITY;

CREATE POLICY budgets_select ON budgets
    FOR SELECT TO vibeforge_app
    USING (tenant_id = current_tenant_id());

CREATE POLICY budgets_insert ON budgets
    FOR INSERT TO vibeforge_app
    WITH CHECK (tenant_id = current_tenant_id());

CREATE POLICY budgets_update ON budgets
    FOR UPDATE TO vibeforge_app
    USING (tenant_id = current_tenant_id())
    WITH CHECK (tenant_id = current_tenant_id());

-- =============================================================================
-- AUDIT_LOG — append-only
-- =============================================================================
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;

CREATE POLICY audit_log_select ON audit_log
    FOR SELECT TO vibeforge_app
    USING (tenant_id = current_tenant_id());

CREATE POLICY audit_log_insert ON audit_log
    FOR INSERT TO vibeforge_app
    WITH CHECK (tenant_id = current_tenant_id());

-- No UPDATE / DELETE policy intentionally omitted → default-deny

-- =============================================================================
-- ESCALATION_REGISTER
-- =============================================================================
ALTER TABLE escalation_register ENABLE ROW LEVEL SECURITY;
ALTER TABLE escalation_register FORCE ROW LEVEL SECURITY;

CREATE POLICY escalation_register_select ON escalation_register
    FOR SELECT TO vibeforge_app
    USING (tenant_id = current_tenant_id());

CREATE POLICY escalation_register_insert ON escalation_register
    FOR INSERT TO vibeforge_app
    WITH CHECK (tenant_id = current_tenant_id());

-- =============================================================================
-- ESCALATION_MEMORY
-- =============================================================================
ALTER TABLE escalation_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE escalation_memory FORCE ROW LEVEL SECURITY;

-- Tenants can read their own entries AND global entries (scope = 'global')
CREATE POLICY escalation_memory_select ON escalation_memory
    FOR SELECT TO vibeforge_app
    USING (tenant_id = current_tenant_id() OR scope = 'global');

CREATE POLICY escalation_memory_insert ON escalation_memory
    FOR INSERT TO vibeforge_app
    WITH CHECK (tenant_id = current_tenant_id());

CREATE POLICY escalation_memory_update ON escalation_memory
    FOR UPDATE TO vibeforge_app
    USING (tenant_id = current_tenant_id())
    WITH CHECK (tenant_id = current_tenant_id());

-- =============================================================================
-- GATE_DECISIONS
-- =============================================================================
ALTER TABLE gate_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE gate_decisions FORCE ROW LEVEL SECURITY;

CREATE POLICY gate_decisions_select ON gate_decisions
    FOR SELECT TO vibeforge_app
    USING (tenant_id = current_tenant_id());

CREATE POLICY gate_decisions_insert ON gate_decisions
    FOR INSERT TO vibeforge_app
    WITH CHECK (tenant_id = current_tenant_id());

-- =============================================================================
-- VERIFICATION QUERIES (commented out — run manually to validate RLS)
--
-- -- Set tenant context:
-- SET LOCAL app.current_tenant_id = '00000000-0000-0000-0000-000000000001';
-- SET ROLE vibeforge_app;
-- SELECT count(*) FROM jobs;  -- should return only rows for that tenant
--
-- -- Test cross-tenant block:
-- SET LOCAL app.current_tenant_id = '00000000-0000-0000-0000-000000000099';
-- SELECT count(*) FROM jobs;  -- should return 0 (no rows for non-existent tenant)
--
-- -- Test append-only enforcement:
-- SET LOCAL app.current_tenant_id = '00000000-0000-0000-0000-000000000001';
-- UPDATE job_events SET event_type = 'tampered' WHERE id = '<some-id>';
-- -- should raise: ERROR: new row violates row-level security policy
-- =============================================================================
