# RAG-Demo — Vier RAG-Strategien über einen Wikipedia-Artikel

Multi-Strategy Retrieval-Augmented-Generation-System, das **dieselbe
Wissensbasis** (`de.wikipedia.org/wiki/Apple`) mit vier unterschiedlichen
Verfahren erschließt und im Frontend nebeneinander vergleichbar macht.

| | Strategie | Idee | Speicher | Doku |
|---|---|---|---|---|
| **UE1** | Simple RAG | Dense (pgvector HNSW) + BM25 (tsvector GIN) + RRF + LLM-Rerank + MMR | Postgres | [📄 MD](docs/UE1/beschreibung.md) · [🌐 HTML](docs/UE1/selbstlernumgebung.html) |
| **UE2** | PageIndex | LLM-gesteuerte Baum-Navigation (Vectify-Ansatz) + gefilterter UE1-Retrieval | Postgres + `ue2.tree_node` | [📄 MD](docs/UE2/beschreibung.md) · [🌐 HTML](docs/UE2/selbstlernumgebung.html) |
| **UE3** | GraphRAG | Microsoft GraphRAG: Property-Graph + Louvain-Communities + local/global/hybrid | Postgres + Neo4j | [📄 MD](docs/UE3/beschreibung.md) · [🌐 HTML](docs/UE3/selbstlernumgebung.html) |
| **UE4** | Ontology-RAG | OWL-Ontologie mit Reasoner, NL→SPARQL, DBpedia-Anreicherung + Live-Fallback | Postgres + Neo4j + GraphDB | [📄 MD](docs/UE4/beschreibung.md) · [🌐 HTML](docs/UE4/selbstlernumgebung.html) |

Eine kompakte Vergleichsmatrix (Stärken, Schwächen, Demo-Queries) liegt in
[`docs/README.md`](docs/README.md).

## Status

Alle vier Strategien sind vollständig implementiert (Daten-Layer, Ingest,
Retrieval, API, Frontend, Tests) und laufen produktiv unter
`rag-apple.butscher.cloud` hinter Traefik.

## Was die Strategien unterscheiden — in 30 Sekunden

- **UE1** beantwortet semantische Volltextfragen schnell und solide
  („Was beschreibt der Artikel über Apples Designphilosophie?").
- **UE2** versteht **Struktur** — geeignet, wenn die Frage nach einem
  bestimmten Abschnitt verlangt („Was steht im Kapitel Geschichte?").
- **UE3** versteht **Beziehungen** zwischen Entitäten und Cluster-Themen
  („Welche Personen sind mit Apple-Produkten verbunden?").
- **UE4** kann **logisch schließen** und externe Datenquellen anbinden
  („Welche Smartphones führt Apple?" → via Subklassen-Inferenz auch ohne
  explizite Auflistung; „Was war vor dem PowerBook 145b?" → Live-Lookup
  in DBpedia, falls lokal nicht vorhanden).

Im Compare-Tab des Frontends werden alle vier Antworten + Quellen + ein
LLM-Judge-Ranking nebeneinander dargestellt.

## Architektur

```
                ┌────────────── Wikipedia Apple (de) ──────────────┐
                │ raw.snapshot  →  clean.section  (Markdown)       │
                └──┬──────────────────────────────────────────────┬─┘
                   │                                              │
                   ▼                                              ▼
        UE1: ue1.chunk + Embedding                 UE2: ue2.tree_node
             (pgvector HNSW, tsvector GIN)              (Hierarchie + Summaries)
                   │
                   ├── UE3: ue3.entity_* + Neo4j Property-Graph + Communities
                   │       (Louvain via networkx)
                   │
                   └── UE4: Ontotext GraphDB Triple-Store + OWL-Horst-Reasoner
                           + DBpedia (validator, products, cross-edges, live)

  Backend  : FastAPI + Gemini 2.5 Flash + gemini-embedding-001 (768-dim Matryoshka)
  Frontend : Vite + React + TypeScript + Tailwind (Aicher/Tufte-Stil)
  Deploy   : Docker Compose + Traefik
```

## Tech-Stack

| Schicht | Komponenten |
|---|---|
| LLM | Google Gemini 2.5 Flash (Chat + NL→SPARQL), `gemini-embedding-001` (768-dim) |
| Vector + BM25 | Postgres 16 + pgvector (HNSW) + tsvector/GIN |
| Property-Graph | Neo4j 5 (Cypher, in-memory Vektor-Index) |
| Triple-Store | Ontotext GraphDB 11 (OWL-Horst-Reasoner, sameAs aktiv) |
| Communities | `networkx` + `python-louvain` |
| External KB | DBpedia (httpx → `https://dbpedia.org/sparql`) |
| Backend | FastAPI, SSE für Streaming-Antworten |
| Frontend | Vite, React 18, TypeScript, Tailwind, react-force-graph-2d |
| Deployment | Docker Compose, Traefik (TLS via Let's Encrypt) |

## Schnellstart (lokal)

```bash
# 1. Credentials (lokal, NICHT committen)
cp .env.example .env
# → GEMINI_API_KEY und Neo4j-Passwort eintragen

# 2. Datenbanken hochfahren
docker compose -f docker-compose.local.yml up -d db uc5-neo4j graphdb

# 3. Schemata + Ontologie
docker compose -f docker-compose.local.yml exec app \
  python -m backend.data.migrate
docker compose -f docker-compose.local.yml exec app \
  python -m backend.data.graphdb_client

# 4. Backend starten
docker compose -f docker-compose.local.yml up app

# 5. Frontend (zweites Terminal)
cd frontend && npm install && npm run dev
# → http://localhost:5173
```

Ingest pro Strategie läuft anschließend über den **Admin-Tab** im Frontend
(oder via `POST /api/{ue1,ue2,ue3,ue4}/ingest`). Anschließend zusätzlich:

```bash
curl -X POST localhost:8000/api/ue4/validate
curl -X POST localhost:8000/api/ue4/enrich-products
curl -X POST localhost:8000/api/ue4/enrich-edges
curl -X POST localhost:8000/api/ue3/sync-canonical
curl -X POST localhost:8000/api/ue3/enrich-cooccurrence
```

## Verzeichnisstruktur

```
backend/
  data/         Datenbank-Clients (Postgres, Neo4j, GraphDB) + Migrations-Runner
  ingest/       Pipelines pro UE + DBpedia-Anreicherungen
  retrieval/    Strategien (simple, pageindex, graphrag, ontology) + Common
  api/          FastAPI-Routen
  main.py       App-Entry
data/
  migrations/postgres/   001_… 007_…
  migrations/neo4j/      Constraints
  migrations/graphdb/    001_apple_ontology.ttl, 003_canonical_persons.ttl
frontend/
  src/components/   Chat, Compare, Graph, Admin
  src/api/          Typed Fetch-Clients + SSE
docs/
  UE1/, UE2/, UE3/, UE4/   beschreibung.md + selbstlernumgebung.html pro Strategie
  README.md                Quick-Compare aller vier Strategien
tests/
  unit/, integration/, e2e/
```

## Demo-Queries (im Compare-Tab)

| Frage | Erwartete Stärke |
|---|---|
| „Welche CEOs hat Apple gehabt?" | UE4 (SPARQL via `apple:CEO`) |
| „Welche Smartphones führt Apple?" | UE4 (Subklassen-Inferenz iPhone) |
| „Wer hat Apple gegründet?" | UE4 (3 Founders) |
| „Was war vor iPhone 4?" | UE4 (`apple:predecessorOf`) |
| „Welche Personen arbeiten an Apple?" | UE3 (PERSON-Entities) |
| „Welcher CEO kam nach Sculley?" | UE3 (nach Sync) + UE4 |
| „Was sagt der Artikel über das Apple-Design?" | UE1, UE2 (semantisch breit) |
| „Was steht im Kapitel Geschichte?" | UE2 (strukturelle Navigation) |

## Lizenz & Quellen

Lehrmaterial im Rahmen der Vorlesung „Datenbasierte Fallstudien"
(THWS, Sommersemester 2026). Wikipedia-Inhalte: CC BY-SA 4.0.
DBpedia-Inhalte: CC BY-SA 3.0.
