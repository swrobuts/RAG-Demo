"""UE4 — Ontology-RAG retrieval against GraphDB.

Pipeline per query:
1. LLM gets the question + the Apple ontology summary and writes a SPARQL
   SELECT/CONSTRUCT query.
2. We execute it against GraphDB (which has OWL/RDFS reasoning enabled, so
   subclass and inverse inferences fire automatically).
3. Result bindings → "chunks" the answering LLM can use as facts. For each
   bound URI we optionally pull a couple of supporting UE1 chunks via name
   match so the final answer can cite the underlying Wikipedia text.

The SPARQL itself is shown in the trace — main didactic payoff of UE4.
"""
from __future__ import annotations

import json
import logging
import re
import time

from sqlalchemy import text

from backend.config import get_settings
from backend.data import graphdb_client
from backend.data.pg import session_scope
from backend.llm.factory import get_chat_llm
from backend.retrieval.base import Chunk, RetrievalResult, SourceRef

log = logging.getLogger(__name__)

# Compact ontology summary passed to the LLM in every query — full TTL is
# ~280 lines and too noisy in a prompt. This is the structural digest.
ONTOLOGY_SUMMARY = """\
PREFIX apple: <http://uc5.butscher.cloud/apple#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX foaf: <http://xmlns.com/foaf/0.1/>

CLASSES (alle mit apple:-Präfix; Hierarchie via ⊑):
  Person ⊐ Employee ⊐ Executive ⊐ CEO
  Person ⊐ Founder
  Person ⊐ Designer
  Person ⊐ Engineer
  Organization ⊐ Company
  Organization ⊐ Shareholder
  Organization ⊐ Supplier
  Product ⊐ HardwareProduct ⊐ Computer ⊐ {Desktop, Notebook}
  Product ⊐ HardwareProduct ⊐ MobileDevice ⊐ {Smartphone, Tablet, Wearable}
  Product ⊐ SoftwareProduct ⊐ OperatingSystem
  Product ⊐ OnlineService
  Product ⊐ ProductFamily
  Event ⊐ Era
  Location, Concept

OBJECT PROPERTIES (Domain → Range):
  apple:foundedBy (Org → Person)        owl:inverseOf apple:founded
  apple:worksFor (Person → Org)         owl:inverseOf apple:hasEmployee
  apple:wasCEOOf (Person → Org)         owl:inverseOf apple:hasCEO
  apple:designedBy (Product → Person)
  apple:manufactures (Org → Product)    owl:inverseOf apple:manufacturedBy
  apple:partOfFamily (Product → ProductFamily, transitive)
  apple:successorOf (Product → Product) owl:inverseOf apple:predecessorOf
  apple:duringEra (Product → Era)
  apple:associatedWith (Symmetric, parent of most other relations)

DATA PROPERTIES:
  apple:foundedYear (Org → xsd:gYear)
  apple:releaseYear (Product → xsd:gYear)
  apple:role (Person → string)
  rdfs:label (multi-lingual labels)
  foaf:name (canonical name string)

KEY INSTANCES:
  apple:AppleInc  a apple:Company  (= dbpedia:Apple_Inc.)

REASONING enabled — Subklassen-Inferenz aktiv: rdf:type-Queries auf
apple:Manager bekommen automatisch alle apple:CEO-Instanzen mit. Auch
inverse Properties werden automatisch ergänzt.
"""

NL_TO_SPARQL_SYSTEM = (
    "Du bist ein SPARQL-1.1-Query-Generator für eine OWL-Ontologie über das "
    "Unternehmen Apple, die OWL-Horst-Reasoning aktiv hat (Subklassen-, "
    "Subproperty- und Inverse-Inferenz). Du bekommst die Ontologie-Struktur "
    "und eine deutsche Frage. Erzeuge eine SELECT-Query. "
    "Halte die Query knapp (max. 20 Zeilen). "
    "Antworte AUSSCHLIESSLICH mit dem SPARQL-Code, ohne Codefence, ohne "
    "Erklärung davor/danach."
)

_NL_TO_SPARQL_BODY = """__ONTOLOGY__

DATEN-REALITÄTSCHECK (wichtig für die Query-Strategie):
- Der GESAMTE Wissensgraph beschreibt das Unternehmen Apple. Es gibt keine
  andere Firma. Du musst Personen/Produkte NICHT zusätzlich zu Apple
  filtern — wer hier ein apple:CEO ist, ist immer ein Apple-CEO. Verwende
  KEINEN apple:associatedWith-Filter zu apple:Apple oder apple:AppleInc;
  der Filter wirft Ergebnisse weg.
- ROLLEN sind sauber als Klassen-Typen modelliert: Person mit CEO-Rolle
  hat ``rdf:type apple:CEO``, Founder hat ``rdf:type apple:Founder``,
  Designer hat ``rdf:type apple:Designer``. Subklassen-Inferenz ist
  aktiv — Query auf apple:Executive liefert auch apple:CEO automatisch.
- Die meisten Beziehungen sind als generisches ``apple:associatedWith``
  abgelegt. Spezifische Properties (apple:wasCEOOf, apple:foundedBy,
  apple:manufactures) existieren in der Ontologie aber sind im Daten
  sehr dünn — verlasse dich auf rdf:type, nicht auf diese Properties.

DATENBEREINIGUNG (wichtig für saubere Ergebnisse):
- Klasse apple:UnrelatedPerson markiert Personen, die im Wikipedia-Artikel
  nur als Kontext erwähnt sind (Alan Turing, Pawel Durow etc.) — IMMER mit
  ``FILTER NOT EXISTS { ?p a apple:UnrelatedPerson }`` ausschließen.
- Kanonische Personen tragen oft @de UND @en Label — mit ``STR(?name)`` oder
  ``FILTER(LANG(?name) = "de" || LANG(?name) = "")`` deduplizieren.

QUERY-STRATEGIE-REGELN (verbindlich):

1. FÜR „WER war/ist X-Rolle" (CEO, Founder, Designer, Engineer, Executive):
     PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
     PREFIX apple: <http://uc5.butscher.cloud/apple#>
     SELECT DISTINCT (STR(?label) AS ?name) WHERE {
       ?p a apple:CEO ;        # oder Founder/Designer/etc.
          rdfs:label ?label .
       FILTER NOT EXISTS { ?p a apple:UnrelatedPerson }
     } LIMIT 30
   NIE einen apple:associatedWith-Filter nach apple:Apple dazu hängen.
   Das ``STR()`` strippt Sprach-Tags, sodass „Tim Cook"@de und „Tim Cook"@en
   zu einem Ergebnis verschmelzen.

2. FÜR „WELCHE Produkte / Personen / Orte" (Typ-Fragen):
     SELECT DISTINCT ?name WHERE {
       ?x rdf:type/rdfs:subClassOf* apple:Product ;
          rdfs:label ?name .
     } LIMIT 30
   Wieder: kein zusätzlicher Apple-Bezug nötig.

3. FÜR „WER steht in Beziehung zu Y" (wo Y ein konkreter Knoten ist):
     SELECT DISTINCT ?label WHERE {
       { Y apple:associatedWith ?p } UNION { ?p apple:associatedWith Y }
       ?p rdfs:label ?label .
     }

4. FÜR PRODUKT-CHRONOLOGIE (Vorgänger / Nachfolger):
   Konvention: ``?A apple:predecessorOf ?B`` heißt „A ist Vorgänger von B"
   (A kommt zuerst, B danach). ``?A apple:successorOf ?B`` heißt
   „A ist Nachfolger von B" (A kommt danach). Beide sind owl:inverseOf.

   Für „Vorgänger von X" — Y suchen mit Y apple:predecessorOf X:
     SELECT DISTINCT (STR(?n) AS ?name) WHERE {
       ?x rdfs:label "iPhone 4"@en .
       ?pred apple:predecessorOf ?x .
       ?pred rdfs:label ?n .
     } LIMIT 10

   Für „Nachfolger von X" — Y suchen mit Y apple:successorOf X (oder
   inverse-aware: X apple:predecessorOf Y):
     SELECT DISTINCT (STR(?n) AS ?name) WHERE {
       ?x rdfs:label "iPhone 4"@en .
       ?succ apple:successorOf ?x .
       ?succ rdfs:label ?n .
     } LIMIT 10

5. FÜR „WER IST X?" / „WAS IST X?" (Profil-Fragen — RICHER columns!):
   Eine einspaltige Antwort wie nur `?fullname` ist hier zu dünn. IMMER
   zusätzlich Typ, Rolle und Beschreibung mitziehen, damit das antwortende
   LLM kontextualisieren kann („Wer ist Gil?" → CEO 1996–1997, vollständiger
   Name etc.). OPTIONAL-Blöcke verwenden, falls einzelne Felder fehlen:
     PREFIX apple: <http://uc5.butscher.cloud/apple#>
     PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
     PREFIX rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
     SELECT DISTINCT
       (GROUP_CONCAT(DISTINCT STR(?lbl); SEPARATOR=" | ") AS ?labels)
       (GROUP_CONCAT(DISTINCT STR(?t); SEPARATOR=", ") AS ?types)
       (SAMPLE(?r)    AS ?role)
       (SAMPLE(?desc) AS ?description)
     WHERE {
       ?p rdfs:label ?match .
       FILTER(REGEX(STR(?match), "Gil", "i"))   # Suchbegriff aus der Frage
       OPTIONAL { ?p rdfs:label ?lbl . }
       OPTIONAL { ?p a ?t . FILTER(STRSTARTS(STR(?t), "http://uc5.butscher.cloud/apple#")) }
       OPTIONAL { ?p apple:role ?r . }
       OPTIONAL { ?p apple:description ?desc . }
       FILTER NOT EXISTS { ?p a apple:UnrelatedPerson }
     }
     GROUP BY ?p LIMIT 5

6. IMMER:
   - rdfs:label für lesbare Namen
   - LIMIT 30 am Ende (oder LIMIT 5 bei Profil-Fragen mit OPTIONAL-Blöcken)
   - PREFIX-Block am Anfang nicht vergessen (apple, rdf, rdfs, foaf, owl)

Frage: __QUERY__

SPARQL:"""


def _build_nl_to_sparql_prompt(query: str) -> str:
    """Use literal placeholders + str.replace() so the curly braces in
    SPARQL examples don't collide with Python's .format() syntax."""
    return (_NL_TO_SPARQL_BODY
            .replace("__ONTOLOGY__", ONTOLOGY_SUMMARY)
            .replace("__QUERY__", query))


class OntologyRAG:
    name = "ue4"

    def __init__(self, llm_provider: str = "gemini") -> None:
        self._chat = get_chat_llm(llm_provider)
        self._llm_provider = llm_provider

    def retrieve(self, query: str, k: int | None = None) -> RetrievalResult:
        settings = get_settings()
        k = k or settings.ue4_top_k_results

        # ── Phase 1: NL → SPARQL ──
        t0 = time.perf_counter()
        sparql = self._generate_sparql(query)
        sparql_gen_ms = (time.perf_counter() - t0) * 1000

        # ── Phase 2: execute against GraphDB ──
        bindings: list[dict] = []
        sparql_err: str | None = None
        t0 = time.perf_counter()
        try:
            result = graphdb_client.select(sparql)
            for b in (result.get("results", {}).get("bindings", []))[:k]:
                row = {k_: v["value"] for k_, v in b.items()}
                bindings.append(row)
        except Exception as exc:  # noqa: BLE001
            sparql_err = str(exc)[:200]
            log.warning("SPARQL execution failed: %s", sparql_err)
        sparql_exec_ms = (time.perf_counter() - t0) * 1000

        # ── Phase 3: turn bindings into chunks + supplementary text chunks ──
        chunks, sources = self._bindings_to_chunks(bindings, query=query)

        # ── Phase 4: text fallback when SPARQL returned nothing ──
        #
        # Real-world gap: the SPARQL the LLM wrote is syntactically fine,
        # but the UE3 extractor only populates apple:associatedWith for
        # 87 % of relations (the specific ones — predecessorOf, designedBy
        # etc. — are nearly empty). So queries like "Vorgängerprodukt vom
        # PowerBook 165" always 0-bind. Rather than just hand the
        # answering LLM an empty result, we fall back to the UE1 hybrid
        # retrieval on the original NL question. The user gets *some*
        # grounded answer from the Wikipedia text instead of "I don't know",
        # and the trace tells them why we had to fall back.
        fallback_used = False
        if not bindings and not sparql_err:
            try:
                from backend.retrieval.simple import SimpleRAG
                fb = SimpleRAG(llm_provider=self._llm_provider).retrieve(query, k=k)
                if fb.chunks:
                    fallback_chunks = [Chunk(
                        text="HINWEIS: Die Ontologie-Query lieferte keine "
                             "Treffer im Knowledge Graph (Beziehung möglicherweise "
                             "nicht populiert). Folgende Textstellen aus dem "
                             "Wikipedia-Artikel enthalten relevante Schlüsselwörter:",
                        section_path="UE4 Text-Fallback",
                        chunk_id=None,
                    )]
                    fallback_chunks.extend(fb.chunks)
                    chunks = fallback_chunks
                    sources = fb.sources
                    fallback_used = True
            except Exception as exc:  # noqa: BLE001
                log.warning("UE4 text fallback failed: %s", exc)

        # ── Phase 5: live DBpedia lookup as third-tier fallback ──
        #
        # If the local SPARQL was empty AND the UE1 text retrieval found
        # nothing useful (just the "HINWEIS" chunk = 1 chunk, or actual
        # 0-result chunks where the answering LLM would still say "I
        # don't know"), try DBpedia directly. Uses the entity labels
        # the LLM put into the SPARQL as anchors. Latency-bounded —
        # 8s timeout, 1 retry, fails silently.
        live_dbpedia_used = False
        needs_live_lookup = (
            not bindings and not sparql_err
            # The text fallback either failed or returned only the marker
            and (not fallback_used or len(chunks) <= 1)
        )
        if needs_live_lookup:
            try:
                from backend.retrieval.dbpedia_live import lookup_to_chunks
                live_chunks, live_sources = lookup_to_chunks(sparql)
                if live_chunks:
                    intro = Chunk(
                        text=("HINWEIS: Weder die lokale Ontologie noch der "
                              "Wikipedia-Apple-Artikel haben Treffer geliefert. "
                              "Folgende Fakten kommen LIVE aus DBpedia:"),
                        section_path="UE4 Live-DBpedia-Fallback",
                        chunk_id=None,
                    )
                    chunks = [intro] + live_chunks
                    sources = live_sources
                    live_dbpedia_used = True
            except Exception as exc:  # noqa: BLE001
                log.warning("UE4 live DBpedia fallback failed: %s", exc)

        trace = {
            "strategy": self.name,
            "llm_provider": self._llm_provider,
            "k": k,
            "sparql": sparql,
            "sparql_err": sparql_err,
            "binding_count": len(bindings),
            "first_bindings": bindings[:5],
            "sparql_gen_ms": round(sparql_gen_ms, 1),
            "sparql_exec_ms": round(sparql_exec_ms, 1),
            "chunk_count": len(chunks),
            "text_fallback_used": fallback_used,
            "live_dbpedia_used": live_dbpedia_used,
        }
        return RetrievalResult(chunks=chunks, sources=sources, trace=trace)

    # ── internals ──────────────────────────────────────────────────────────

    def _generate_sparql(self, query: str) -> str:
        prompt = _build_nl_to_sparql_prompt(query)
        try:
            raw, _u = self._chat.generate(NL_TO_SPARQL_SYSTEM, prompt)
        except Exception as exc:  # noqa: BLE001
            log.warning("SPARQL generation LLM failed: %s", exc)
            return ""
        sparql = _strip_codefence(raw or "")
        # Defensive: many models forget to repeat the PREFIX block even when
        # we put it in the ontology summary. Auto-prepend any prefix we
        # actually use that's missing from the query.
        return _ensure_prefixes(sparql)

    def _bindings_to_chunks(
        self, bindings: list[dict], query: str = "",
    ) -> tuple[list[Chunk], list[SourceRef]]:
        """Format SPARQL bindings as readable chunks. Also pull supplementary
        UE1 chunks for (a) each distinct entity name from the bindings and
        (b) proper-noun-ish tokens from the original NL query — so the
        answering LLM has both structured triple-facts AND Wikipedia context.

        Hebel B+C: When the bindings are "thin" (= only labels, no apple:role
        / description / type column), we double the per-query cap and lean
        harder on the query terms — that's exactly the "Wer ist Gil?" case
        where the SPARQL came back with just "Gilbert Frank Amelio" and the
        article only mentions him as "Gil Amelio".
        """
        if not bindings:
            return [], []

        # Build a primary "facts" chunk listing the bindings — that's the
        # core evidence the answering LLM uses.
        lines = ["Strukturierte Fakten aus dem Knowledge Graph:"]
        for i, b in enumerate(bindings, start=1):
            parts = [f"{k}={v}" for k, v in b.items()]
            lines.append(f"  {i}. {' | '.join(parts)}")
        facts_chunk = Chunk(
            text="\n".join(lines),
            section_path="UE4 SPARQL-Ergebnis",
            chunk_id=None,
        )
        chunks: list[Chunk] = [facts_chunk]

        # Hebel C — "thin binding" heuristic: if no column hints at a rich
        # property (description/role/type/comment/abstract), the answering LLM
        # would have only labels. Augment harder in that case.
        rich_keys = {"desc", "description", "comment", "abstract",
                     "role", "type", "types"}
        is_thin = not any(
            any(k.lower() in rich_keys for k in b.keys()) for b in bindings
        )

        # Collect candidate names (any binding value that's not a URI and
        # has reasonable length).
        names: list[str] = []
        for b in bindings:
            for v in b.values():
                if not isinstance(v, str):
                    continue
                if v.startswith("http"):
                    continue
                v = v.strip()
                if 2 <= len(v) <= 80 and v not in names:
                    names.append(v)
                if len(names) >= 10:
                    break
            if len(names) >= 10:
                break

        # Hebel B — also feed proper-noun-ish tokens from the query itself
        # into the UE1 search. This is what closes the "Gilbert Frank Amelio"
        # vs "Gil Amelio" gap: the SPARQL pulled the long name, but the
        # German article only ever mentions the short form.
        for term in _extract_query_anchors(query):
            if term not in names:
                names.append(term)
            if len(names) >= 14:
                break

        # Pull supplementary UE1 chunks for those names. Cap doubles for
        # thin bindings so the answering LLM gets enough context.
        settings = get_settings()
        per_entity = settings.ue4_max_chunks_per_entity
        cap = per_entity * (8 if is_thin else 4)
        if names:
            params: dict[str, object] = {"cap": cap}
            ilike_conds = []
            for i, n in enumerate(names):
                params[f"n{i}"] = f"%{n}%"
                ilike_conds.append(f"c.text ILIKE :n{i}")
            with session_scope() as session:
                rows = session.execute(
                    text(
                        f"SELECT c.id, s.path AS section, c.text "
                        f"FROM ue1.chunk c LEFT JOIN clean.section s ON s.id = c.section_id "
                        f"WHERE {' OR '.join(ilike_conds)} "
                        f"ORDER BY c.id LIMIT :cap"
                    ),
                    params,
                ).mappings().all()
            for r in rows:
                chunks.append(Chunk(
                    text=r["text"], section_path=r["section"], chunk_id=int(r["id"]),
                ))

        sources = [
            SourceRef(
                chunk_id=c.chunk_id, section_path=c.section_path,
                text=c.text, distance=None,
            )
            for c in chunks
        ]
        return chunks, sources


# Common German wh-words/auxiliaries that look proper-noun-ish to a naive
# regex but carry no entity content. Excluded from anchor extraction.
_QUERY_STOPWORDS = {
    "Wer", "Wie", "Was", "Wo", "Wann", "Warum", "Welche", "Welcher",
    "Welches", "Wessen", "Wem", "Wen",
    "Der", "Die", "Das", "Den", "Dem", "Des",
    "Ein", "Eine", "Einen", "Einem", "Einer", "Eines",
    "Und", "Oder", "Aber", "Nicht", "Auch", "Noch", "Schon",
    "Sein", "Seine", "Ihr", "Ihre", "Mein", "Meine", "Dein", "Deine",
    "Name", "Vollständiger", "Vollständige", "Vollständigen",
    "Apple", "Apples",  # ubiquitous in this corpus → useless as anchor
}


def _extract_query_anchors(query: str) -> list[str]:
    """Pull proper-noun-ish tokens out of an NL query for UE1 augmentation.

    For German questions we want capitalised content words (≥3 chars) that
    aren't generic stopwords. Also picks up known short names like "Gil"
    that wouldn't pass a longer cutoff. Returns max 6 candidates, ordered
    by appearance.
    """
    if not query:
        return []
    # Tokens: capitalised word with 2+ trailing word chars, supports umlauts/ß.
    raw = re.findall(r"\b[A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9]{1,}\b", query)
    out: list[str] = []
    for tok in raw:
        if tok in _QUERY_STOPWORDS:
            continue
        if tok in out:
            continue
        out.append(tok)
        if len(out) >= 6:
            break
    return out


def _strip_codefence(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


# Standard prefixes our ontology uses. Auto-prepended to LLM-generated SPARQL
# if missing — Gemini sometimes forgets them despite the instruction.
_KNOWN_PREFIXES = {
    "apple":  "http://uc5.butscher.cloud/apple#",
    "rdf":    "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs":   "http://www.w3.org/2000/01/rdf-schema#",
    "owl":    "http://www.w3.org/2002/07/owl#",
    "foaf":   "http://xmlns.com/foaf/0.1/",
    "schema": "http://schema.org/",
    "xsd":    "http://www.w3.org/2001/XMLSchema#",
    "dbo":    "http://dbpedia.org/ontology/",
    "dbr":    "http://dbpedia.org/resource/",
}


def _ensure_prefixes(sparql: str) -> str:
    """Prepend PREFIX declarations for any of the known prefixes that
    appear in the query body but aren't already declared at the top.
    Without this, even a perfect SELECT trips GraphDB with HTTP 400."""
    if not sparql:
        return sparql
    head_lower = sparql.lower()
    missing: list[str] = []
    for prefix, uri in _KNOWN_PREFIXES.items():
        used = re.search(rf"(?<![A-Za-z0-9_]){prefix}:", sparql)
        if not used:
            continue
        declared = re.search(
            rf"prefix\s+{prefix}\s*:",
            head_lower,
        )
        if not declared:
            missing.append(f"PREFIX {prefix}: <{uri}>")
    if not missing:
        return sparql
    return "\n".join(missing) + "\n" + sparql
