# UE1 — Simple RAG (Hybrid Retrieval)

## Was diese Strategie macht

UE1 ist die Basisform von Retrieval-Augmented Generation: ein **deutscher
Wikipedia-Artikel über Apple** wird abschnittsweise zerlegt, jedes
Textstück erhält einen Vektor-Embedding, eine SQL-Volltextsuche-Indizierung,
und bei jeder Frage wird das relevanteste Material gefunden, mit einem
LLM-Reranker gefiltert, mit MMR diversifiziert, und an Gemini 2.5 Flash
als Kontext für die Antwort übergeben. Es ist die Referenz, gegen die
UE2–UE4 verglichen werden.

## Konzeptuelle Architektur

```
Wikipedia (de)
     │  fetch_article()
     ▼
┌──────────────┐
│  raw.snapshot│  ← rohes HTML
└──────┬───────┘
       │  clean()
       ▼
┌──────────────┐
│ clean.section│  ← strukturierte Sektionen mit Pfad
└──────┬───────┘
       │  chunker()  +  Section-Prefix
       ▼
┌──────────────┐     ┌──────────────────┐
│  ue1.chunk   │ →   │ Postgres + pgvector│
└──────┬───────┘     │  • text             │
                     │  • embedding(768)   │
       Embedding     │  • tsvector(GIN)    │
       (Gemini       └──────────────────┘
       gemini-
       embedding-001)

QUERY
  │
  ▼
  Hybrid Retrieval = Dense (pgvector) ⊕ Sparse (BM25/tsvector)
       │ Reciprocal Rank Fusion (RRF)
       ▼
  Top-K Candidates
       │ LLM-Reranker (Gemini)
       ▼
  Top-N Reranked
       │ MMR Diversification
       ▼
  Final Chunks
       │
       ▼
  Answer Prompt → Gemini 2.5 Flash → Antwort
```

## Daten-Layer

| Schema | Tabelle | Inhalt |
|---|---|---|
| `raw` | `snapshot` | URL, ETag, fetched_at, rohes HTML |
| `clean` | `document` | id, snapshot_id, title |
| `clean` | `section` | id, document_id, path (z.B. „Geschichte/1976–1980: Gründung"), depth, parent_id, body_md |
| `ue1` | `chunk` | id, section_id, ordinal, text, embedding (vector(768)), text_tsv (tsvector) |
| `meta` | `ingest_run` | id, strategy, snapshot_id, started/finished, stats JSONB |

**Indizes:**
- `ue1.chunk` → HNSW(embedding vector_cosine_ops, m=16, ef_construction=64)
- `ue1.chunk` → GIN(text_tsv) für BM25
- Migration `006_…_tsvector_gin.sql` legt das alles an

## Ingest-Pipeline (`backend/ingest/ue1_simple.py`)

1. **Snapshot lesen** — Inhalt aus `raw.snapshot` (oder vorher via
   `fetch_article()` von `https://de.wikipedia.org/wiki/Apple` holen)
2. **HTML cleanen** — MediaWiki-Wrapper, Infoboxen, Navigation, Referenzen
   entfernen. Der `mw-heading`-Wrapper braucht Sonderbehandlung — wir
   suchen mit `_is_heading_wrapper()` + `_effective_start()` durch
3. **Sektionen extrahieren** — H2/H3-Headings → hierarchische Pfade
   wie `Geschichte/1985–1996: Sculley-Ära und danach`
4. **Chunken** — Markdown-Sektionen werden in Token-budget-Chunks
   geschnitten (~500 Tokens), wobei jedem Chunk der **Section-Path
   als Prefix vorangestellt** wird. Das verbessert das Embedding:
   ein Chunk über Steve Jobs in der Sektion „Gründung" hat anderen
   semantischen Kontext als einer in „Krise"
5. **Embedden** — Batch-Aufrufe an Google `gemini-embedding-001` mit
   `output_dimensionality=768` und L2-Normalisierung (Matryoshka-
   Embedding). Pro Chunk ein 768-dim Vektor
6. **tsvector erzeugen** — Postgres `to_tsvector('german', text)`
   automatisch via Trigger oder Generated Column
7. **In `ue1.chunk` schreiben** — id, section_id, ordinal, text,
   embedding, text_tsv

## Retrieval-Pipeline (`backend/retrieval/simple.py` + `pipeline.py`)

Bei jeder Query passiert das:

1. **Query-Embedding** — gleiches Modell wie beim Ingest
2. **Dense Retrieval** — `ORDER BY embedding <=> :q_emb LIMIT 30`
   (cosine-distance via pgvector)
3. **Sparse Retrieval (BM25)** — `to_tsquery('german', :keywords)`
   matched gegen den GIN-Index, ranked via `ts_rank_cd`
4. **Reciprocal Rank Fusion** — beide Listen werden über
   `RRF(d) = Σ 1/(60+rank_i)` zusammengeführt → Top-15 Kandidaten
5. **LLM-Reranker** — Gemini bewertet jedes Kandidaten-Chunk auf
   Relevanz zur Query (1–10), Top-K nach LLM-Score
6. **MMR-Diversification** — λ·relevance − (1−λ)·max_sim_to_selected,
   wählt die K finalen Chunks so dass sie unter sich semantisch
   verschieden sind (gegen redundante Treffer)

## Answering (`backend/api/routes.py` → `_run_strategy_and_answer`)

- System-Prompt: „Du beantwortest die Frage AUSSCHLIESSLICH auf Basis
  der Auszüge. Zitiere Section-Pfade in Klammern."
- User-Prompt: Frage + ausgewählte Chunks
- Modell: Gemini 2.5 Flash (`gemini-2.5-flash`) oder LM Studio
  (Gemma 3) als lokale Alternative
- Wenn keine Chunks zurückkommen: skipped_llm=true, Standard-Antwort
  „Keine Auszüge gefunden, bitte Frage spezifischer formulieren"

## Eingesetzte Tools

| Tool | Version | Zweck |
|---|---|---|
| Python | 3.12 | Backend |
| FastAPI | 0.115 | HTTP-Schicht |
| SQLAlchemy | 2.0 | ORM/Migrations |
| Alembic | 1.14 | Schema-Migrations |
| Postgres | 16 | Hauptspeicher |
| pgvector | 0.8 | Vektor-Index (HNSW) |
| httpx | 0.27 | Wikipedia-Fetch |
| Gemini API | 2.5-flash, embedding-001 | LLM + Embeddings |
| markdownify | 0.13 | HTML → MD |
| BeautifulSoup4 | 4.12 | HTML-Parsing |

## Stärken

- **Sehr robust** — Hybrid-Retrieval kompensiert die Schwächen von Dense
  Embeddings bei Eigennamen / Zahlen / seltenen Begriffen
- **Antworten mit Quellen** — jeder zitierte Section-Pfad ist nachprüfbar
- **Einfache Wartung** — eine Tabelle, ein Embedding-Modell, ein Index
- **Latenz < 5 s** für die meisten Queries

## Grenzen

- **Kann nicht abstrahieren** — wenn die Antwort über mehrere Sektionen
  zusammengesetzt werden muss, scheitert UE1
- **Kein strukturiertes Verständnis** — „Wer waren alle CEOs von Apple?"
  zerfällt in einzelne Treffer ohne Listen-Logik
- **Sektionsstruktur ignoriert** — die Hierarchie aus `clean.section`
  wird zum Ingest-Zeitpunkt benutzt (Prefix), beim Retrieval aber nicht.
  Das macht UE2 (PageIndex) anders

## Demo-Queries

| Query | UE1-Verhalten |
|---|---|
| „Wann wurde Apple gegründet?" | ✅ findet die Sektion „Gründung" direkt |
| „Was war 1997 die Krise?" | ✅ liefert die Sculley-Ära-Sektion |
| „Welches war das erste iPhone?" | ✅ Section „Geschichte" + Embedding |
| „Welche CEOs hatte Apple?" | ⚠️ findet einzelne Erwähnungen, kein klares Listing |
| „Welches Produkt war vor dem PowerBook 165?" | ❌ Nischenmodell nicht im Artikel |

## Quellen im Code

- `backend/ingest/ue1_simple.py` — Ingest
- `backend/retrieval/simple.py` — Strategy-Klasse
- `backend/retrieval/pipeline.py` — Hybrid + Rerank + MMR
- `data/migrations/postgres/001…006_*.sql` — Schemas + Indizes
- `tests/unit/test_pipeline.py` — Tests für Reranker + MMR
