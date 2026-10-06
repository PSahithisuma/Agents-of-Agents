-- RLS and append-only enforcement test script
-- Run via: docker compose exec -T postgres psql -U vibeforge -d vibeforge -f /tmp/rls_test.sql

\echo '=== Step 1: Seed test data as superuser ==='

INSERT INTO users (id, tenant_id, keycloak_sub, email, role)
VALUES (
  'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
  '00000000-0000-0000-0000-000000000001',
  'test-kc-sub-001',
  'test@vibeforge.local',
  'developer'
) ON CONFLICT (keycloak_sub) DO NOTHING;

INSERT INTO projects (id, tenant_id, owner_id, name)
VALUES (
  '11111111-1111-1111-1111-111111111111',
  '00000000-0000-0000-0000-000000000001',
  'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
  'RLS Test Project'
) ON CONFLICT (id) DO NOTHING;

INSERT INTO application_specs (id, tenant_id, project_id, created_by, version)
VALUES (
  '22222222-2222-2222-2222-222222222222',
  '00000000-0000-0000-0000-000000000001',
  '11111111-1111-1111-1111-111111111111',
  'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
  1
) ON CONFLICT (id) DO NOTHING;

INSERT INTO jobs (id, tenant_id, spec_id, idempotency_key)
VALUES (
  '33333333-3333-3333-3333-333333333333',
  '00000000-0000-0000-0000-000000000001',
  '22222222-2222-2222-2222-222222222222',
  'test-idempotency-key-001'
) ON CONFLICT (idempotency_key) DO NOTHING;

\echo '=== Step 2: Test INSERT into job_events as vibeforge_app (should succeed) ==='

SET app.current_tenant_id = '00000000-0000-0000-0000-000000000001';
SET ROLE vibeforge_app;

INSERT INTO job_events (tenant_id, job_id, event_type, payload)
VALUES (
  '00000000-0000-0000-0000-000000000001',
  '33333333-3333-3333-3333-333333333333',
  'progress',
  '{"pct": 10, "msg": "Starting..."}'
);

SELECT 'job_events INSERT: OK ✓' AS result;

\echo '=== Step 3: Test UPDATE on job_events (should FAIL — append-only) ==='

\set ON_ERROR_STOP off
UPDATE job_events SET event_type = 'tampered' WHERE job_id = '33333333-3333-3333-3333-333333333333';
\set ON_ERROR_STOP on

\echo '=== Step 4: Test cross-tenant isolation ==='

SET app.current_tenant_id = '00000000-0000-0000-0000-000000000099';

SELECT count(*) AS jobs_for_nonexistent_tenant FROM jobs;
SELECT count(*) AS events_for_nonexistent_tenant FROM job_events;

RESET ROLE;

\echo '=== Step 5: Verify seed tenant exists ==='

SELECT id, name, slug, tier FROM tenants;
SELECT spent_usd, budget_usd FROM budgets;

\echo '=== ALL TESTS COMPLETE ==='
