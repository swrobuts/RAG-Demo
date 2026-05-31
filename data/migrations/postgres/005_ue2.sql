-- ue2: PageIndex-style tree of LLM summaries, used by UE2 to pre-filter
-- the candidate space before classic top-k vector search on ue1.chunk.
CREATE SCHEMA IF NOT EXISTS ue2;

CREATE TABLE IF NOT EXISTS ue2.tree_node (
    id            BIGSERIAL PRIMARY KEY,
    document_id   BIGINT NOT NULL REFERENCES clean.document(id) ON DELETE CASCADE,
    parent_id     BIGINT REFERENCES ue2.tree_node(id) ON DELETE CASCADE,
    section_id    BIGINT REFERENCES clean.section(id) ON DELETE SET NULL,
    level         INT NOT NULL,
    heading       TEXT NOT NULL,
    path          TEXT NOT NULL,
    order_idx     INT NOT NULL,
    summary       TEXT NOT NULL,
    text          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS tree_node_document_idx
    ON ue2.tree_node (document_id, order_idx);
CREATE INDEX IF NOT EXISTS tree_node_parent_idx
    ON ue2.tree_node (parent_id);
CREATE INDEX IF NOT EXISTS tree_node_section_idx
    ON ue2.tree_node (section_id);
CREATE INDEX IF NOT EXISTS tree_node_path_idx
    ON ue2.tree_node (document_id, path text_pattern_ops);
