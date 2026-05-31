-- ue3: GraphRAG vector-side tables. The graph proper lives in Neo4j;
-- here we store summaries with their embeddings so we can do fast vector
-- search over entities and communities without leaving Postgres.
CREATE SCHEMA IF NOT EXISTS ue3;

CREATE TABLE IF NOT EXISTS ue3.entity_summary (
    entity_key    TEXT PRIMARY KEY,        -- canonical "{type}:{normalized_name}" mirrors Neo4j Entity.id
    name          TEXT NOT NULL,
    type          TEXT NOT NULL,           -- PERSON | ORGANIZATION | PRODUCT | EVENT | LOCATION | CONCEPT
    description   TEXT NOT NULL,
    mention_count INT NOT NULL DEFAULT 0,
    embedding     vector(768)
);
CREATE INDEX IF NOT EXISTS entity_summary_type_idx ON ue3.entity_summary (type);
CREATE INDEX IF NOT EXISTS entity_summary_embedding_idx
    ON ue3.entity_summary USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS ue3.community_summary (
    community_id  TEXT PRIMARY KEY,
    level         INT NOT NULL,
    size          INT NOT NULL,
    summary       TEXT NOT NULL,
    entity_keys   TEXT[] NOT NULL,
    embedding     vector(768)
);
CREATE INDEX IF NOT EXISTS community_summary_level_idx ON ue3.community_summary (level);
CREATE INDEX IF NOT EXISTS community_summary_embedding_idx
    ON ue3.community_summary USING hnsw (embedding vector_cosine_ops);
