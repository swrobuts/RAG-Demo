# UC5 RAG-Demo — Dokumentation

Vier Retrieval-Strategien über demselben Wikipedia-Artikel (Apple Inc.,
de.wikipedia.org). Pro Strategie gibt es eine technisch ausführliche
Beschreibung (`beschreibung.md`, Grundlage für Folien) und eine
Selbstlernumgebung (`selbstlernumgebung.html`, schrittweise
Implementierungs-Anleitung).

| Strategie | Kurzbeschreibung | Beschreibung | Selbstlernumgebung |
|---|---|---|---|
| **UE1 – Simple RAG** | Dense + BM25 + RRF + Rerank + MMR | [MD](UE1/beschreibung.md) | [HTML](UE1/selbstlernumgebung.html) |
| **UE2 – PageIndex** | LLM-Tree-Navigation + gefilterter UE1 | [MD](UE2/beschreibung.md) | [HTML](UE2/selbstlernumgebung.html) |
| **UE3 – GraphRAG** | Property-Graph + Louvain + 3 Modi | [MD](UE3/beschreibung.md) | [HTML](UE3/selbstlernumgebung.html) |
| **UE4 – Ontology-RAG** | OWL + SPARQL + DBpedia | [MD](UE4/beschreibung.md) | [HTML](UE4/selbstlernumgebung.html) |

## Quick-Compare

| Aspekt | UE1 | UE2 | UE3 | UE4 |
|---|---|---|---|---|
| Persistenz | Postgres + pgvector | + `ue2.tree_node` | + Neo4j | + GraphDB Triples |
| Hauptindex | HNSW (768-dim) + GIN (tsvector) | Hierarchischer Baum | Property-Graph | OWL Triples + Reasoner |
| Retrieval | Hybrid (RRF + LLM-Rerank + MMR) | Tree-Walk → UE1 | local/global/hybrid + Text-Fallback | SPARQL → UE1 → DBpedia |
| LLM-Aufrufe (typ.) | 2 (Rerank + Antwort) | 3-5 (Tree + Antwort) | 1 (Antwort) | 2 (NL→SPARQL + Antwort) |
| Stärke | Schnell, semantisch breit | Strukturelles Verständnis | Inter-Entity-Kontext | Reasoning, externe Anbindung |
| Schwäche | „Wer/Was war erster X?" schlecht | Tree-Quality abh. von Doc-Struktur | Extraktions-Qualität | NL→SPARQL fragil |

## Querying im Vergleich

Demo-Frage „Welcher CEO kam nach Sculley?":

- **UE1** Hybrid-Retrieval findet zwar Textstellen, aber keine
  strukturierte Antwort
- **UE2** Tree navigiert zur Sektion „Geschichte", liefert breiten
  Kontext
- **UE3** Property-Graph kennt `Sculley` und `Spindler`, kann aber
  ohne `succeededBy`-Kante nicht direkt antworten — Text-Fallback hilft
- **UE4** SPARQL über `apple:predecessorOf` / `apple:successorOf`
  liefert exakte Antwort

Demo-Frage „Welche Smartphones führt Apple?":

- **UE4** ist konkurrenzlos: `?x rdf:type/rdfs:subClassOf* apple:Smartphone`
  liefert via Subklassen-Inferenz alle iPhone-Modelle
- **UE1-3** finden zwar Erwähnungen, aber keine vollständige Liste

## Architektur-Überblick

```
┌─────────────── Wikipedia Apple (de) ───────────────┐
│ raw.snapshot → clean.section (Markdown-Sections)   │
└──────┬───────────────────────────────────┬─────────┘
       │                                   │
       ▼                                   ▼
  UE1: ue1.chunk + Embedding         UE2: ue2.tree_node
       │   (Postgres pgvector)            (Hierarchie + Summary)
       │
       ├── UE3: ue3.entity + Neo4j Graph + Communities
       │
       └── UE4: GraphDB Triple-Store + OWL Reasoner + DBpedia

  Backend: FastAPI + Gemini 2.5 Flash + gemini-embedding-001
  Frontend: Vite + React + Tailwind (Aicher-Stil)
  Deploy: Docker Compose + Traefik @ rag-apple.butscher.cloud
```

## Verwandte Dokumente (Top-Level Repo)

- `README.md` — Quickstart, ENV-Variablen, Deploy
- `docs/architecture.md` — Gesamtarchitektur
- `docs/data-model.md` — Schema-Migrationen
- `docs/runbook.md` — Häufige Ops-Probleme
