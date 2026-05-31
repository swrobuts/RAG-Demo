# UE4 — Ontology-RAG (OWL + SPARQL + DBpedia)

## Was diese Strategie macht

UE4 ist die strikteste der vier Strategien: eine **OWL-Ontologie über Apple**
(28 Klassen mit Hierarchie, ~15 Properties) liegt in der Triple-Store
**Ontotext GraphDB 11** mit aktivierter **OWL-Horst-Reasoner** (Subklassen-,
Subproperty- und Inverse-Inferenz). UE3-Entitäten werden in diese Ontologie
eingespielt. Bei jeder Frage schreibt ein LLM eine **SPARQL-Query** gegen
die Ontologie, das Ergebnis sind strukturierte Tripel.

Drei Fallback-Stufen sorgen für Robustheit:
1. SPARQL gegen GraphDB
2. Wenn 0 Bindings → UE1-Text-Retrieval
3. Wenn weiter nichts → **Live-DBpedia-Lookup** für die in der SPARQL
   genannten Entitäten

## Konzeptuelle Architektur

```
data/migrations/graphdb/
  001_apple_ontology.ttl        (28 Klassen + Properties)
  002_seed_aliases.ttl          (owl:sameAs)
  003_canonical_persons.ttl     (Tim Cook, Sculley, Spindler, Amelio, …)
       │
       │ upload via /repositories/.../statements
       ▼
  ┌────────────────────────┐
  │ GraphDB Repository     │ ← OWL-Horst-Reasoner aktiv
  │ uc5_rag_apple          │ ← entity-index-size 10M
  └─────────┬──────────────┘
            │
       UE3 Ingest (Bridge)
            │
       Anreicherungen (DBpedia)
       ─────────────────────────
       • dbpedia_validator   → Personen + UnrelatedPerson
       • dbpedia_products    → Produkte + Chronologie
       • dbpedia_edges       → Cross-Edges zwischen UE3-Entities

QUERY ("Was war vor dem PowerBook 145b?")
  │
  ▼
  Phase 1: NL → SPARQL via Gemini
  ─────────────────────────
  PREFIX apple: <http://uc5.butscher.cloud/apple#>
  SELECT (STR(?l) AS ?name) WHERE {
    ?x rdfs:label "PowerBook 145b"@en .
    ?pred apple:predecessorOf ?x .
    ?pred rdfs:label ?l .
  } LIMIT 30
  │
  ▼
  Phase 2: Execute against GraphDB (mit Reasoning)
  │
  │ 0 Bindings? ──┐
  ▼              │
  Bindings →     │
  Chunks         │
                 ▼
                 Phase 3: UE1 Text-Fallback
                 │ Keine relevanten Chunks? ──┐
                 ▼                            │
                 chunks                       │
                                              ▼
                                              Phase 4: Live-DBpedia
                                              ?s rdfs:label CONTAINS "PowerBook 145b"
                                              + dbo:predecessor + dbo:successor
                                              + dbo:manufacturer + rdfs:comment
                                              │
                                              ▼
                                              chunks
  │
  ▼
  Gemini 2.5 Flash → Antwort
```

## Daten-Layer

### GraphDB Repository `uc5_rag_apple`

Ruleset: `owl-horst-optimized`, sameAs aktiv.

| Bestand (live) | Anzahl |
|---|---:|
| Triples insgesamt | ~3 700 |
| OWL-Klassen | 56 |
| `apple:Person` (mit Subklassen) | 25 |
| `apple:Product` (mit Subklassen) | 102 |
| CEOs (`apple:CEO`) | 5 |
| Founders (`apple:Founder`) | 4 |
| Designers (`apple:Designer`) | 3 |
| `apple:predecessorOf` | 33 |
| `apple:successorOf` | 33 |
| `apple:associatedWith` (UE3-derived) | 402 |
| `owl:sameAs` → DBpedia | 85 |

### Klassenhierarchie (Auszug)

```
Person ⊐ Employee ⊐ Executive ⊐ CEO
Person ⊐ Founder
Person ⊐ Designer
Person ⊐ Engineer
Organization ⊐ Company
Product ⊐ HardwareProduct ⊐ Computer ⊐ {Desktop, Notebook}
Product ⊐ HardwareProduct ⊐ MobileDevice ⊐ {Smartphone, Tablet, Wearable}
Product ⊐ SoftwareProduct ⊐ OperatingSystem
Event ⊐ Era
```

Mit OWL-Horst-Reasoning: eine Query auf `apple:Executive` liefert
**automatisch** alle `apple:CEO`-Instanzen mit (Subklassen-Inferenz).

### Properties (Auszug)

```
apple:foundedBy (Org → Person)     owl:inverseOf apple:founded
apple:wasCEOOf  (Person → Org)     owl:inverseOf apple:hasCEO
apple:designedBy (Product → Person)
apple:manufactures (Org → Product) owl:inverseOf apple:manufacturedBy
apple:predecessorOf (Product → Product) owl:inverseOf apple:successorOf
apple:associatedWith (Symmetric, parent of most other relations)
```

## Retrieval-Pipeline (`backend/retrieval/ontology.py`)

### Phase 1 — NL → SPARQL

System-Prompt (gekürzt):
```
Du erzeugst SPARQL-1.1-Queries für eine OWL-Ontologie über Apple
mit aktivem OWL-Horst-Reasoning.

DATEN-REALITÄTSCHECK:
- Der gesamte Graph beschreibt Apple. Filter NIE zusätzlich
  apple:associatedWith apple:AppleInc — wirft Treffer weg.
- Rollen sind Klassen-Typen (?p a apple:CEO).
- Subklassen-Inferenz aktiv → apple:Executive bekommt CEOs gratis.

QUERY-STRATEGIE-REGELN:
1. „Wer war/ist X-Rolle":
   SELECT DISTINCT (STR(?label) AS ?name) WHERE {
     ?p a apple:CEO ; rdfs:label ?label .
     FILTER NOT EXISTS { ?p a apple:UnrelatedPerson }
   } LIMIT 30

4. PRODUKT-CHRONOLOGIE:
   ?A apple:predecessorOf ?B   heißt „A ist Vorgänger von B".
   Für „Vorgänger von X":
     ?pred apple:predecessorOf ?x . ?pred rdfs:label ?n .
```

Output: rohes SPARQL, durch `_strip_codefence` + `_ensure_prefixes`
gesäubert.

### Phase 2 — Execute

```python
result = graphdb_client.select(sparql)
for b in result["results"]["bindings"][:k]:
    bindings.append({k: v["value"] for k, v in b.items()})
```

Bindings werden zu strukturierten Chunks formatiert + optional
ergänzt durch UE1-Mention-Chunks für jeden geneideten Namen
(„unterstützender Text").

### Phase 3 — Text-Fallback

Wenn `not bindings and not sparql_err`:
- `SimpleRAG.retrieve(query, k)` läuft
- Chunks bekommen einen HINWEIS-Prefix: „Die Ontologie-Query lieferte
  keine Treffer, folgende Wikipedia-Textstellen enthalten Treffer:"

### Phase 4 — Live-DBpedia

(`backend/retrieval/dbpedia_live.py`)

Wenn auch UE1 dünn / leer:
- `extract_anchors(sparql)` zieht Literale aus dem SPARQL („PowerBook 145b")
- Pro Anker: `?s rdfs:label CONTAINS ?anchor` + OPTIONAL predecessor/
  successor/manufacturer/comment gegen `https://dbpedia.org/sparql`
- 8-Sekunden-Timeout, 1 Retry
- Treffer → formatierte Chunks („DBpedia-Eintrag: ... Vorgänger: ...")

## Anreicherungs-Endpoints

| Endpoint | Was passiert | Stats |
|---|---|---|
| `POST /api/ue4/validate` | Personen-Validator: DBpedia kanonische Personen einfügen + Off-Topic demoten | 5 fetched, +1 (Levinson), 11 demoted |
| `POST /api/ue4/enrich-products` | Apple-Produkte + Predecessor/Successor aus DBpedia | 43 fetched, +33, +21/19 |
| `POST /api/ue4/enrich-edges` | DBpedia-Cross-Edges zwischen UE3-Entitäten | 56 edges |
| `POST /api/ue3/sync-canonical` | GraphDB-only Entitäten in Postgres+Neo4j syncen | 62 inserted |
| `POST /api/ue3/enrich-cooccurrence` | Section-Co-Occurrence-Edges | 4374 paare ≥ 2 |

## Eingesetzte Tools

| Tool | Zweck |
|---|---|
| Ontotext GraphDB 11.2 | Triple-Store mit Reasoning |
| `rdflib` | TTL-Parser-Validierung im Test |
| `httpx` | DBpedia SPARQL über HTTP |
| Gemini 2.5 Flash | NL→SPARQL Generation |
| `owl-horst-optimized` Ruleset | Subklassen + Inverse + Subproperty + Transitive |
| DBpedia (`dbpedia.org/sparql`) | Externe Knowledge Base |

## Stärken

- **Strukturiertes Reasoning** — „alle Smartphones" liefert via
  Subklassen-Inferenz automatisch iPhone 4, iPad mini etc.
- **Multi-Source-Antworten** — kombiniert Lokal-Triple, UE1-Text und
  Live-DBpedia
- **Auditierbar** — die SPARQL ist sichtbar, jeder Fakt nachvollziehbar
- **Inverse-Reasoning** — `apple:founded` automatisch aus `apple:foundedBy`

## Grenzen

- **Datenabdeckung** ist immer noch beschränkt — DBpedia hat nicht
  jedes Nischenprodukt
- **NL→SPARQL** kann an semantischer Richtung patzen
  (predecessorOf vs successorOf)
- **DBpedia ist flaky** — Live-Lookup hat Timeouts in Stoßzeiten

## Demo-Queries

| Query | UE4-Verhalten |
|---|---|
| „Welche CEOs hat Apple gehabt?" | ✅ SPARQL → 5 Bindings (Jobs, Cook, Sculley, Spindler, Amelio) |
| „Welche Smartphones führt Apple?" | ✅ Subklassen-Inferenz → 7 iPhone-Modelle |
| „Wer hat Apple gegründet?" | ✅ 3 founders (Jobs, Wozniak, Wayne) |
| „Was war vor iPhone 4?" | ✅ Predecessor-Triple → iPhone 3GS |
| „Was war vor PowerBook 145b?" | ⚠️ Lokal nichts → UE1-Fallback (Modell nicht im Artikel) → Live-DBpedia |

## Quellen im Code

- `backend/retrieval/ontology.py` — Hauptstrategie mit 4 Phasen
- `backend/retrieval/dbpedia_live.py` — Phase-4 Live-Lookup
- `backend/data/graphdb_client.py` — GraphDB REST-Wrapper
- `backend/ingest/ue4_ontology.py` — UE3 → GraphDB Bridge
- `backend/ingest/dbpedia_validator.py` — Personen-Anreicherung
- `backend/ingest/dbpedia_products.py` — Produkt-Chronologie
- `backend/ingest/dbpedia_edges.py` — Cross-Edges
- `data/migrations/graphdb/001_apple_ontology.ttl` — Ontologie
- `data/migrations/graphdb/003_canonical_persons.ttl` — Kanonische Personen
- `tests/unit/test_ue4_ontology.py` — URI + Property-Mapping Tests
- `tests/unit/test_dbpedia_*.py` — Anreicherungs-Tests
