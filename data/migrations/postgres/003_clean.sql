-- clean: normalised Markdown and the section tree of each document.
CREATE SCHEMA IF NOT EXISTS clean;

CREATE TABLE IF NOT EXISTS clean.document (
    id           BIGSERIAL PRIMARY KEY,
    snapshot_id  BIGINT NOT NULL REFERENCES raw.wikipedia_snapshot(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    markdown     TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (snapshot_id)
);

CREATE TABLE IF NOT EXISTS clean.section (
    id           BIGSERIAL PRIMARY KEY,
    document_id  BIGINT NOT NULL REFERENCES clean.document(id) ON DELETE CASCADE,
    parent_id    BIGINT REFERENCES clean.section(id) ON DELETE CASCADE,
    level        INT NOT NULL,
    heading      TEXT NOT NULL,
    path         TEXT NOT NULL,
    order_idx    INT NOT NULL,
    text         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS section_document_idx
    ON clean.section (document_id, order_idx);
CREATE INDEX IF NOT EXISTS section_parent_idx
    ON clean.section (parent_id);
