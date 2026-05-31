-- ue1: chunks with embeddings for simple top-k retrieval.
CREATE SCHEMA IF NOT EXISTS ue1;

CREATE TABLE IF NOT EXISTS ue1.chunk (
    id           BIGSERIAL PRIMARY KEY,
    document_id  BIGINT NOT NULL REFERENCES clean.document(id) ON DELETE CASCADE,
    section_id   BIGINT REFERENCES clean.section(id) ON DELETE SET NULL,
    order_idx    INT NOT NULL,
    text         TEXT NOT NULL,
    token_count  INT,
    embedding    vector(768)
);

CREATE INDEX IF NOT EXISTS chunk_document_idx
    ON ue1.chunk (document_id, order_idx);

-- HNSW for cosine similarity on the Gemini text-embedding-004 vectors.
CREATE INDEX IF NOT EXISTS chunk_embedding_idx
    ON ue1.chunk USING hnsw (embedding vector_cosine_ops);
