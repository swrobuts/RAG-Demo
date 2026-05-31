-- pgvector extension and the meta schema with the migration ledger.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS meta;

CREATE TABLE IF NOT EXISTS meta.schema_migration (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS meta.ingest_run (
    id           BIGSERIAL PRIMARY KEY,
    strategy     TEXT NOT NULL,           -- 'ue1' | 'ue2' | 'ue3'
    snapshot_id  BIGINT NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ,
    status       TEXT NOT NULL,           -- 'running' | 'ok' | 'failed'
    stats        JSONB DEFAULT '{}'::jsonb,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS ingest_run_strategy_idx
    ON meta.ingest_run (strategy, started_at DESC);
