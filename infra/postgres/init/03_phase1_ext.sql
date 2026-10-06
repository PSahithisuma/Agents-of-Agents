-- =============================================================================
-- 03_phase1_ext.sql — Phase 1 extension: RAG ingestion tracking table
--
-- Runs on first Postgres boot (lexicographic order, after 02_rls.sql).
-- Also safe to apply as a manual migration on an existing DB:
--   docker compose exec postgres psql -U vibeforge -d vibeforge -f /docker-entrypoint-initdb.d/03_phase1_ext.sql
-- =============================================================================

-- =============================================================================
-- INGESTED_DOCS
-- Tracks every MinIO object that has been processed by the ingestion service.
-- Prevents re-ingestion on every poll cycle.
-- Convention: object_key = {tenant_id}/{filename}
-- =============================================================================
CREATE TABLE IF NOT EXISTS ingested_docs (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    bucket      TEXT        NOT NULL,
    object_key  TEXT        NOT NULL,
    doc_name    TEXT        NOT NULL,
    chunk_count INTEGER     NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Prevent re-ingestion of the same object
    UNIQUE (bucket, object_key)
);

CREATE INDEX IF NOT EXISTS idx_ingested_docs_tenant   ON ingested_docs (tenant_id);
CREATE INDEX IF NOT EXISTS idx_ingested_docs_key      ON ingested_docs (bucket, object_key);
CREATE INDEX IF NOT EXISTS idx_ingested_docs_at       ON ingested_docs (tenant_id, ingested_at DESC);

-- Grant DML to app role
GRANT SELECT, INSERT, UPDATE ON ingested_docs TO vibeforge_app;

-- =============================================================================
-- RLS on ingested_docs
-- =============================================================================
ALTER TABLE ingested_docs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingested_docs FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON ingested_docs
    USING (
        tenant_id = current_setting('app.current_tenant_id', true)::uuid
    );

-- =============================================================================
-- LITELLM database (used by LiteLLM proxy for key management)
-- Note: The litellm database itself is created by 00_create_dbs.sh.
-- LiteLLM runs its own migrations on startup — nothing to do here.
-- =============================================================================

-- =============================================================================
-- Seed: Add ingested_docs for dev tenant (empty, just ensures table exists)
-- =============================================================================
-- (No seed data needed — ingestion service populates this automatically)
