-- BM25 sibling of pgvector for hybrid retrieval. A tsvector column with a
-- GIN index lets us run Postgres' text-search ranker (ts_rank_cd) over the
-- same chunk rows the dense pgvector index sits on. The trigger keeps tsv
-- in sync with the text body on every insert/update so the application
-- never has to do it.
ALTER TABLE ue1.chunk ADD COLUMN IF NOT EXISTS tsv tsvector;

UPDATE ue1.chunk SET tsv = to_tsvector('german', COALESCE(text, ''))
  WHERE tsv IS NULL;

CREATE INDEX IF NOT EXISTS chunk_tsv_idx ON ue1.chunk USING GIN(tsv);

CREATE OR REPLACE FUNCTION ue1.chunk_tsv_update() RETURNS trigger AS $$
BEGIN
  NEW.tsv := to_tsvector('german', COALESCE(NEW.text, ''));
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS chunk_tsv_trigger ON ue1.chunk;
CREATE TRIGGER chunk_tsv_trigger
BEFORE INSERT OR UPDATE OF text ON ue1.chunk
FOR EACH ROW EXECUTE FUNCTION ue1.chunk_tsv_update();
