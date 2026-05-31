"""Live DBpedia lookup as third-tier fallback for UE4.

Order of escalation when answering a UE4 question:
  1. Local SPARQL against our GraphDB         (fast, only what UE3+enrichments populated)
  2. UE1 hybrid text retrieval on the question (Wiki article about Apple)
  3. Live DBpedia query for entities named in the SPARQL  ← this module

Triggers only when the previous two return nothing. Extracts the
entity labels the NL→SPARQL LLM put as literals (``"PowerBook 145b"@en``
etc.) and asks DBpedia for matching entities + their chronology
properties.

DBpedia is famously flaky; we use a short timeout and a single retry,
fail silently on transport errors — UE4 still degrades gracefully to
"keine Bindings"."""
from __future__ import annotations

import logging
import re

import httpx

from backend.retrieval.base import Chunk, SourceRef

log = logging.getLogger(__name__)

DBPEDIA_SPARQL = "https://dbpedia.org/sparql"
USER_AGENT = (
    "UC5-RAG-Apple/1.0 "
    "(educational demo; https://github.com/swrobuts/Dashboard_sample)"
)
_HTTP_HEADERS = {
    "Accept": "application/sparql-results+json",
    "User-Agent": USER_AGENT,
}


def _dbpedia_query(sparql: str, *, timeout: float = 8.0,
                   retries: int = 1) -> dict:
    """Short-timeout DBpedia query with one retry. Returns {} on any
    transport failure — caller must tolerate empty results."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(timeout=timeout, headers=_HTTP_HEADERS) as c:
                r = c.get(DBPEDIA_SPARQL, params={"query": sparql})
                r.raise_for_status()
                return r.json()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    log.warning("DBpedia live lookup failed: %s", last_exc)
    return {}


# Find every literal in a SPARQL query — these are the labels the LLM
# inserted, the "anchor" entities of the user's question.
_LITERAL_RE = re.compile(r'"((?:[^"\\]|\\.)+)"(?:@\w+)?')


def extract_anchors(sparql: str) -> list[str]:
    """Returns the literal strings the LLM used as label anchors in the
    query. De-duplicated, order preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _LITERAL_RE.finditer(sparql or ""):
        s = m.group(1).strip()
        if len(s) < 3 or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _escape_literal(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def lookup_entity(label: str) -> list[dict]:
    """Returns list of DBpedia facts about every entity matching ``label``.

    Each dict carries: uri, label, comment, predecessor (uri+label),
    successor (uri+label), abstract — whichever fields DBpedia has.
    Empty list if DBpedia has nothing or is unreachable."""
    if not label or len(label.strip()) < 3:
        return []
    esc = _escape_literal(label)
    # Two-pass: first find candidate URIs by partial label match, then
    # one extra query for properties of each. To keep latency bounded
    # we do it as a single query with OPTIONALs.
    q = f"""
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX dbo:  <http://dbpedia.org/ontology/>
PREFIX dbp:  <http://dbpedia.org/property/>
SELECT DISTINCT ?s ?l ?comment
       ?pred ?predLabel ?succ ?succLabel
       ?manuf ?manufLabel
WHERE {{
  ?s rdfs:label ?l .
  FILTER(LANG(?l) = "en")
  FILTER(CONTAINS(LCASE(STR(?l)), LCASE("{esc}")))
  OPTIONAL {{ ?s rdfs:comment ?comment . FILTER(LANG(?comment) = "en") }}
  OPTIONAL {{ {{ ?s dbo:predecessor ?pred }} UNION {{ ?s dbp:predecessor ?pred }}
              ?pred rdfs:label ?predLabel . FILTER(LANG(?predLabel) = "en") }}
  OPTIONAL {{ {{ ?s dbo:successor ?succ }} UNION {{ ?s dbp:successor ?succ }}
              ?succ rdfs:label ?succLabel . FILTER(LANG(?succLabel) = "en") }}
  OPTIONAL {{ ?s dbo:manufacturer ?manuf .
              ?manuf rdfs:label ?manufLabel . FILTER(LANG(?manufLabel) = "en") }}
}}
LIMIT 8
"""
    res = _dbpedia_query(q)
    by_uri: dict[str, dict] = {}
    for b in res.get("results", {}).get("bindings", []):
        uri = b["s"]["value"]
        item = by_uri.setdefault(uri, {
            "uri": uri, "label": b["l"]["value"],
            "comment": None,
            "predecessor": None, "predecessor_label": None,
            "successor": None,   "successor_label": None,
            "manufacturer": None, "manufacturer_label": None,
        })
        if "comment" in b and item["comment"] is None:
            item["comment"] = b["comment"]["value"]
        if "pred" in b and item["predecessor"] is None:
            item["predecessor"] = b["pred"]["value"]
            item["predecessor_label"] = b["predLabel"]["value"]
        if "succ" in b and item["successor"] is None:
            item["successor"] = b["succ"]["value"]
            item["successor_label"] = b["succLabel"]["value"]
        if "manuf" in b and item["manufacturer"] is None:
            item["manufacturer"] = b["manuf"]["value"]
            item["manufacturer_label"] = b["manufLabel"]["value"]
    return list(by_uri.values())


def lookup_to_chunks(sparql: str) -> tuple[list[Chunk], list[SourceRef]]:
    """High-level helper: extract anchor labels from the SPARQL the LLM
    generated, hit DBpedia for each, format the facts as readable chunks
    for the answering LLM."""
    anchors = extract_anchors(sparql)
    if not anchors:
        return [], []
    chunks: list[Chunk] = []
    for label in anchors:
        entries = lookup_entity(label)
        if not entries:
            continue
        for e in entries:
            lines = [
                f"DBpedia-Eintrag: {e['label']}",
                f"  URI: {e['uri']}",
            ]
            if e["manufacturer_label"]:
                lines.append(f"  Hersteller: {e['manufacturer_label']}")
            if e["predecessor_label"]:
                lines.append(f"  Vorgänger: {e['predecessor_label']}")
            if e["successor_label"]:
                lines.append(f"  Nachfolger: {e['successor_label']}")
            if e["comment"]:
                lines.append(f"  Beschreibung: {e['comment'][:400]}")
            chunks.append(Chunk(
                text="\n".join(lines),
                section_path=f"DBpedia · live · {label}",
                chunk_id=None,
            ))
    sources = [
        SourceRef(
            chunk_id=None, section_path=c.section_path,
            text=c.text, distance=None,
        )
        for c in chunks
    ]
    return chunks, sources
