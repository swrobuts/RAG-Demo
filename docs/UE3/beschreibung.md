# UE3 — GraphRAG (Property-Graph + Communities)

## Was diese Strategie macht

UE3 implementiert **Microsoft GraphRAG**: aus dem Wikipedia-Korpus werden
Entitäten + Relationen mit einem LLM extrahiert, im **Neo4j Property-Graph**
abgelegt, mit **Louvain-Community-Detection** in thematische Cluster
gruppiert, und jedes Cluster bekommt eine LLM-Zusammenfassung.

Bei jeder Frage gibt es drei Retrieval-Modi:
- **local** — Entity-anchored: finde die genannte Entität, hole ihre
  Mention-Chunks + 1-Hop-Nachbarn
- **global** — Community-anchored: finde die thematisch passende
  Community via Community-Embedding, gib die Summary zurück
- **hybrid** (Default) — beides zusammen, dedupliziert

Zusätzlich: **Text-Fallback** auf UE1-Hybrid-Retrieval wenn der Graph
keine passenden Anker findet (z.B. weil die UE3-Extraktion eine Entität
verpasst hat — siehe Gil-Amelio-Case).

## Konzeptuelle Architektur

```
UE1.chunk (Wikipedia-Text)
       │
       │ Ingest UE3 (pro Chunk)
       ▼
  Gemini Few-Shot Extractor
  ─────────────────────────
  { entities: [{name,type,description}…],
    relations: [{source,target,type,evidence}…] }
       │
       │ Resolution (Levenshtein, Synonym-Map)
       ▼
  ┌────────────────────┐      ┌────────────────────┐
  │ Postgres           │      │ Neo4j              │
  │ ue3.entity_summary │      │ Entity (id, name,  │
  │   id, name, type,  │      │   type, embedding) │
  │   mention_count,   │      │ MENTIONS (chunk)   │
  │   description      │      │ RELATED_TO         │
  │                    │      │   (type, weight)   │
  │ ue3.entity_mention │      │                    │
  │   entity_key,      │      │ Community labels   │
  │   chunk_id         │      │   via Louvain      │
  └────────────────────┘      └────────────────────┘
       │
       │ Community Detection (Louvain mit networkx)
       ▼
  ue3.community_summary
    community_id, level, size, summary, entity_keys[]

QUERY
  │
  ▼
  ┌─── local ─────┐  ┌─── global ─────┐
  │ Entity-Match  │  │ Community-Embed│
  │ → 1-Hop       │  │ → Top-Summary  │
  └───────────────┘  └────────────────┘
       │   ⊕             │
       ▼                 ▼
       Merge + dedupe
       │
       │ Wenn 0 / dünn / nur CANON → Text-Fallback (UE1)
       ▼
       Final Chunks → Gemini → Antwort
```

## Daten-Layer

| Speicher | Inhalt |
|---|---|
| **Postgres `ue3.entity_summary`** | id, entity_key (`PRODUCT:macintosh`), name, type, description, mention_count |
| **Postgres `ue3.entity_mention`** | entity_key, chunk_id (Mapping Entität ↔ UE1-Chunk) |
| **Postgres `ue3.community_summary`** | community_id, level, size, summary, entity_keys[] (Array) |
| **Neo4j `Entity`-Nodes** | id (entity_key), name, type, source, embedding |
| **Neo4j `MENTIONS`-Edge** | Entity → Chunk-id (via Postgres) |
| **Neo4j `RELATED_TO`-Edge** | (a)-[r:RELATED_TO {type, weight, source}]->(b) |

Migration: `007_ue3_entities_communities.sql` legt die Postgres-Tabellen
an; Neo4j-Constraints via `data/migrations/neo4j/001_constraints.cypher`.

## Ingest-Pipeline (`backend/ingest/ue3_graphrag.py`)

### Schritt 1 — Entity + Relation Extraction (pro Chunk)

Prompt-Auszug:
```
Du bist ein gründlicher Named-Entity- und Relations-Extraktor für ein
Knowledge-Graph-System über Apple.

BESONDERS WICHTIG — historische CEOs werden oft übersehen, MÜSSEN aber
extrahiert werden wenn im Text:
  • CEOs: Steve Jobs, John Sculley, Michael Spindler, Gil Amelio, Tim Cook
  • Founder: Steve Wozniak, Ronald Wayne, Mike Markkula, Jef Raskin
  • Designer: Jonathan Ive (Jony Ive), Hartmut Esslinger
  • Executives: Phil Schiller, Craig Federighi, Eddy Cue, ...

Typen: PERSON, ORGANIZATION, PRODUCT, EVENT, LOCATION, CONCEPT.
Antworte AUSSCHLIESSLICH mit gültigem JSON.
```

Output:
```json
{
  "entities": [
    {"name": "Steve Jobs", "type": "PERSON", "description": "Mitgründer, CEO 1997–2011"},
    {"name": "Apple I", "type": "PRODUCT", "description": "Erster Computer von Apple, 1976"}
  ],
  "relations": [
    {"source": "Steve Wozniak", "target": "Apple I",
     "type": "ENTWICKELT", "evidence": "Wozniak entwickelte den Apple I"}
  ]
}
```

### Schritt 2 — Resolution

- Personen-Aliase: „Wozniak" → „Steve Wozniak" (Levenshtein-Match auf
  letztes Wort + Type-Constraint)
- Case-Insensitive Dedup
- Apple-Konflikt: „Apple" (UE3 extracts) ↔ „Apple Inc." (DBpedia) → später
  via `owl:sameAs` in UE4 gemerged

### Schritt 3 — Persist

- `ue3.entity_summary` mit `mention_count` aggregiert
- Neo4j `Entity`-Nodes + `RELATED_TO`-Edges mit Type aus Extraktion
- `MENTIONS`-Edges entity_key → chunk_id via Postgres

### Schritt 4 — Embeddings für Entities

Pro Entity: `name + " - " + description` → Gemini Embedding (768-dim) →
in `Entity.embedding` (Neo4j-Vector-Index oder gespeichert für späteren
Match)

### Schritt 5 — Community Detection (Louvain)

```python
import networkx as nx
import community  # python-louvain

G = nx.Graph()
for edge in related_to_edges:
    G.add_edge(edge.src, edge.tgt, weight=edge.weight)

partition = community.best_partition(G)  # {entity_key: cluster_id}
```

### Schritt 6 — Community-Summaries

Pro Cluster: Member-Namen + Top-Mention-Chunks → Gemini → 2-Satz-Summary.
Speichern in `ue3.community_summary`.

## Retrieval-Pipeline (`backend/retrieval/graphrag.py`)

```python
def retrieve(query, k=8, mode="hybrid"):
    keywords = extract_keywords(query)
    type_hints = extract_type_hints(query)
    q_emb = embed(query)
    
    local_chunks, matched_entities = _local_retrieve(q_emb, keywords, type_hints, k)
    global_chunks, matched_comms = _global_retrieve(q_emb, k)
    
    merged = dedupe([*local_chunks, *global_chunks])[:k]
    
    # Augment-Fallback:
    if not merged or thin(merged) or any_CANON or chunks_dont_match_query:
        text_chunks = SimpleRAG.retrieve(query, k)
        merged = merged + dedupe_against(merged, text_chunks)
    
    return RetrievalResult(merged, sources, trace)
```

### `_local_retrieve`

1. Finde Entities via Name-Match (PostgreSQL ILIKE) + Type-Hint
2. Holen ihre `MENTIONS`-Edges → Postgres-Chunks (volltext)
3. Optional: 1-Hop-Nachbarn via Neo4j Cypher

### `_global_retrieve`

1. Embedde alle Community-Summaries (1x bei Ingest)
2. Top-K Communities nach Cosinus-Distanz
3. Return die Summaries als „Chunk"

### Text-Fallback (ab Commit `7c9fbcd`/`244d9b3`/`6b783e3`)

Triggers wenn:
- `merged` ist leer
- ODER `len(merged) < k/2` (thin match)
- ODER ANY matched_entity ist `CANON:*` (kanonisch, keine Mention-Chunks)
- ODER keine Chunks enthalten Query-Wörter

Dann: UE1-Hybrid-Retrieval mit dem Original-Query, augmentiert (nicht
ersetzt) die Graph-Chunks.

## Anreicherungen

### DBpedia Cross-Edges (`backend/ingest/dbpedia_edges.py`)
Für jede UE3-Entität mit `owl:sameAs` zu DBpedia: holt alle DBpedia-
Triples zwischen unseren Entities und fügt sie als Neo4j-Edges ein
(z.B. `Steve Jobs → founded_by → Apple Inc.`).

### Wikipedia-Co-Occurrence (`backend/ingest/cooccurrence.py`)
Für jedes Paar von Entitäten zählt die Sektionen, in denen beide
erwähnt werden. Bei ≥2 → Neo4j-Edge `mentioned_with` mit
`weight = count`. Verdichtet den Graph erheblich (4400+ Paare).

### Canonical → UE3 Sync (`backend/ingest/canonical_to_ue3.py`)
Pusht jede GraphDB-only Entität (DBpedia-Validator, Produkte etc.)
als `CANON:<localname>`-Knoten in Postgres + Neo4j. Schließt die
Sichtbarkeitslücke (Gil Amelio jetzt auch UE3-anker-bar).

## Eingesetzte Tools

| Tool | Zweck |
|---|---|
| Neo4j 5 | Property-Graph |
| networkx + python-louvain | Community Detection |
| Postgres `ue3.*` Tabellen | Entity Metadata, Mentions |
| Gemini 2.5 Flash | Extraction + Summarization |
| `gemini-embedding-001` | Entity + Community Embeddings |
| react-force-graph-2d | Frontend-Visualisierung |

## Stärken

- **Strukturierte Fragen** („Welche Personen arbeiten an Apple?")
  bekommen sauberes Listing
- **Inter-Entity-Kontext** sichtbar im Frontend-Graph
- **Communities** geben „Übersicht" zu einem Themenbereich
- **Text-Fallback** macht UE3 nie schlechter als UE1

## Grenzen

- **Extraktions-Qualität** ist die Achillesferse — verpasste Entitäten
  bleiben unsichtbar (siehe Gil Amelio vor dem schärferen Prompt)
- **87 % der Relationen sind generisch** `associatedWith` — nur ~20
  spezifische `predecessorOf`/`founded`/`designedBy`. Wurde via
  DBpedia-Anreicherung verbessert
- **Communities sind Black-Boxes** — Louvain-Cluster sind nicht immer
  semantisch intuitiv

## Demo-Queries

| Query | UE3-Verhalten |
|---|---|
| „Welche Personen arbeiten an Apple?" | ✅ local-retrieve findet PERSON-Entitäten + Mention-Chunks |
| „Was ist die Hauptidee von Apples Produktdesign?" | ✅ global-retrieve trifft die Concept-Community |
| „Welcher CEO kam nach Sculley?" | ✅ (nach Sync) findet Spindler → Amelio → Cook über `predecessorOf` |
| „Gil Amelio" | ✅ (nach Text-Fallback) augmentiert mit UE1-Wiki-Text |

## Quellen im Code

- `backend/ingest/ue3_graphrag.py` — Hauptingest
- `backend/retrieval/graphrag.py` — Strategy + Fallbacks
- `backend/ingest/dbpedia_edges.py` — Cross-Edges
- `backend/ingest/cooccurrence.py` — Section-Co-Occurrence
- `backend/ingest/canonical_to_ue3.py` — Sync nach UE3
- `data/migrations/postgres/007_ue3_*.sql` — Schemas
- `data/migrations/neo4j/001_constraints.cypher` — Neo4j-Setup
- `tests/unit/test_graphrag_extractor.py` — JSON-Parse Tests
