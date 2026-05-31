-- raw: untouched Wikipedia snapshots (one row per fetched revision).
CREATE SCHEMA IF NOT EXISTS raw;

CREATE TABLE IF NOT EXISTS raw.wikipedia_snapshot (
    id            BIGSERIAL PRIMARY KEY,
    url           TEXT NOT NULL,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    etag          TEXT,
    revision_id   TEXT,
    html          TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    UNIQUE (url, content_hash)
);

CREATE INDEX IF NOT EXISTS wikipedia_snapshot_url_idx
    ON raw.wikipedia_snapshot (url, fetched_at DESC);
