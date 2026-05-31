# UE2 — PageIndex (Tree-Navigation + Simple RAG)

## Was diese Strategie macht

UE2 erweitert UE1 um eine **LLM-gestützte Tree-Navigation** im Stil von
[Vectify PageIndex](https://github.com/VectifyAI/PageIndex). Der Wikipedia-
Artikel wird zusätzlich zu den Chunks als **hierarchischer Baum von
zusammengefassten Sektionen** abgelegt. Bei einer Frage navigiert ein LLM
zuerst durch den Baum (wie ein Mensch durch ein Inhaltsverzeichnis) und
dispatcht erst danach zur eigentlichen UE1-Retrieval-Pipeline mit den
ausgewählten Sektionen als Filter.

Vorteil: Übersichts-/Strukturfragen („Was passierte in der Sculley-Ära?")
landen punktgenau im richtigen Abschnitt, statt sich auf semantische
Ähnlichkeit zu verlassen.

## Konzeptuelle Architektur

```
UE1 Output (clean.section + ue1.chunk)
         │
         │ (Ingest UE2)
         ▼
  Bottom-Up Summarization
  ─────────────────────────
  Leaf-Sektionen  →  Gemini  →  Kurz-Zusammenfassung
  Eltern-Sektion  →  Gemini  +  Kind-Summaries  →  Zwischen-Summary
  Wurzel          →  Gemini  +  alle Kind-Summaries  →  Top-Summary
         │
         ▼
  ue2.tree_node (id, parent_id, section_id?, summary, depth, path)

QUERY
  │
  ▼
  Phase 1 — Tree Navigation
  ─────────────────────────
  LLM bekommt Wurzel-Summary + Top-Level Kinder
       │ wählt Pfad → vertieft
       ▼ rekursiv bis Tiefe N oder Leaf
  Selected Leaf-Sektion(en)
  │
  ▼
  Phase 2 — Filtered UE1 Retrieval
  ─────────────────────────
  Dense + BM25 + Rerank + MMR  beschränkt auf section_id ∈ Selected
  │
  ▼
  Final Chunks → Antwort mit Gemini
```

## Daten-Layer

| Schema | Tabelle | Zweck |
|---|---|---|
| `ue2` | `tree_node` | id, parent_id, section_id (nullable für Synth-Knoten), summary, depth, path |

Migration: `005_ue2_tree.sql` legt die rekursive Tabelle an.

Beispiel-Eintrag:
```
id=42, parent_id=10, section_id=87, depth=2,
path="Geschichte/1985-1996: Sculley-Ära und danach",
summary="Sculley übernimmt 1983 die Führung, ersetzt Steve Jobs in einem
        Machtkampf. Apple verliert Marktanteile, drei CEO-Wechsel
        (Sculley → Spindler → Amelio) bis 1997. Endet mit dem Aufkauf
        von NeXT und Steve Jobs' Rückkehr als Interims-CEO."
```

## Ingest-Pipeline (`backend/ingest/ue2_pageindex.py`)

1. **Voraussetzung**: UE1-Ingest ist bereits gelaufen (Sektionen +
   Chunks vorhanden)
2. **Sektion-Baum aus `clean.section` rekonstruieren** — depth, parent_id
3. **Bottom-Up Summarization**:
   - Pro Leaf-Sektion: alle Chunks → Gemini → 2-3-Satz-Zusammenfassung
   - Pro Eltern-Sektion: Liste der Kind-Summaries (+ optional eigener
     Body) → Gemini → eine Ebene höher
   - Wurzel: Gesamtüberblick über den ganzen Artikel
4. **Speichern in `ue2.tree_node`** — gleiche path-Konvention wie
   `clean.section`, plus die LLM-erzeugte `summary`
5. **Idempotenz**: jeder Knoten wird per (snapshot_id, section_path)
   versioniert; Re-Run überschreibt nur Knoten älter als der Snapshot

## Retrieval-Pipeline (`backend/retrieval/pageindex.py`)

### Phase 1 — Tree-Navigation

```python
prompt = f"""
Du navigierst durch ein Wikipedia-Inhaltsverzeichnis.
Aktuelle Position: {node.path}
Zusammenfassung dieser Sektion: {node.summary}

Kindknoten:
  1. {child1.path} — {child1.summary[:120]}
  2. {child2.path} — {child2.summary[:120]}
  ...

Frage: {query}

Welche Kindknoten enthalten höchstwahrscheinlich die Antwort?
Antworte mit JSON: {{"selected": [1,3], "dive_deeper": true|false}}
"""
```

- Start an der Wurzel
- LLM wählt 1-N relevante Kinder
- Wenn `dive_deeper` und Tiefe < max: rekursiv
- Sonst: aktuelle Auswahl als „terminal nodes" zurückgeben

**Halluzinations-Schutz**: das LLM darf nur Indizes der gezeigten Kinder
zurückgeben; ungültige Antworten werden gefiltert und führen zu „pick
first sibling" als Fallback.

### Phase 2 — Filtered UE1-Retrieval

Für die ausgewählten Terminal-Sektionen wird die UE1-Pipeline
(Dense + BM25 + Rerank + MMR) gestartet, aber mit
`WHERE section_id IN (...selected_ids)` als Filter.

## Eingesetzte Tools

Gleicher Stack wie UE1, plus:

| Tool | Zweck |
|---|---|
| Gemini 2.5 Flash | Summarization (Ingest) + Navigation (Retrieval) |
| JSON Schema Validation | sichere Parse der Navigations-Antworten |

## Stärken

- **Übersichts-Fragen** („Was passierte in Phase X?") landen im
  korrekten Abschnitt
- **LLM-Calls bleiben moderat** — Navigation = log N statt N (Anzahl
  Sektionen)
- **Wenn UE1 schon läuft, baut UE2 nur eine zweite Schicht obendrauf**

## Grenzen

- **Wenn die Summaries den Begriff nicht enthalten, scheitert die
  Navigation** — z.B. „Gil Amelio" steht nicht im Sculley-Ära-Summary,
  also wird die Sektion nicht ausgewählt
- **Latenz höher als UE1** — eine zusätzliche LLM-Rundenfolge zur
  Navigation (~2 s)
- **Tree muss bei jedem Snapshot regeneriert werden** — die Summaries
  beziehen sich auf konkrete Section-Texte

## Demo-Queries

| Query | UE2 vs. UE1 |
|---|---|
| „Wer hat Apple gegründet?" | gleich gut, Navigation findet „Gründung"-Sektion |
| „Was war 1997 die Krise?" | UE2 ist genauer — landet direkt in „Sculley-Ära" |
| „Welche Produkte kamen 2007 raus?" | UE2 navigiert in „Produkte"-Sektion |
| „Welcher CEO kam nach Spindler?" | abhängig vom Summary-Inhalt — falls Amelio nicht im Summary, scheitert |

## Quellen im Code

- `backend/ingest/ue2_pageindex.py` — Bottom-Up Summarization
- `backend/retrieval/pageindex.py` — Navigation + Dispatch
- `data/migrations/postgres/005_ue2_tree.sql` — Schema
- `tests/unit/test_pageindex_parser.py` — Tests für JSON-Output-Parse
- `tests/unit/test_pageindex_terminal_nodes.py` — Tests für die
  Terminal-Node-Auswahl
