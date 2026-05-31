"""Ground UC5 entity extractions against DBpedia.

The UE3 entity extractor produces false positives (persons referenced only
in context — Alan Turing, Aljaksandr Lukaschenka, Isaac Newton, …) and
false negatives (Tim Cook, John Sculley, Michael Spindler etc. never
extracted). This module uses DBpedia as ground-truth oracle to clean
both directions:

Step 1 — *enrichment*: pull canonical Apple-related persons from DBpedia
        (``dbo:founder``, ``dbo:keyPerson`` of ``dbr:Apple_Inc.``) and
        insert any we don't already have, with correct type + ``owl:sameAs``
        link back to DBpedia.

Step 2 — *verification*: for every person currently typed as ``apple:Person``
        that lacks an Apple-specific role (CEO/Founder/Designer/…), ask
        DBpedia whether the same English label has any direct relation to
        ``dbr:Apple_Inc.``. If yes, leave alone; if no, demote to
        ``apple:UnrelatedPerson`` so role-queries stay clean.

The whole pass is idempotent — safe to re-run, no duplicates.

Didactic payoff for the course: same NL→SPARQL pipeline as UE4, but now
using DBpedia as a **second** knowledge base to validate the first. This
is exactly the "linked-data leverage" Berners-Lee envisioned — your
extractor is wrong, so you cross-check against a curated public KB.
"""
from __future__ import annotations

import logging
import re
import time

import httpx

from backend.data import graphdb_client

log = logging.getLogger(__name__)

DBPEDIA_SPARQL = "https://dbpedia.org/sparql"
USER_AGENT = (
    "UC5-RAG-Apple/1.0 "
    "(educational demo; https://github.com/swrobuts/Dashboard_sample)"
)
APPLE_NS = "http://uc5.butscher.cloud/apple#"

_HTTP_HEADERS = {
    "Accept": "application/sparql-results+json",
    "User-Agent": USER_AGENT,
}


# ── HTTP plumbing ─────────────────────────────────────────────────────────

def _dbpedia_query(sparql: str, *, timeout: float = 30.0) -> dict:
    """Run a SPARQL SELECT/ASK against the public DBpedia endpoint.

    Returns ``{}`` on transport error (logged at WARNING) so the caller
    can decide whether the missing answer is fatal or just degrades a
    single step. DBpedia is famously flaky during European business
    hours — we never block UE4 on it."""
    try:
        with httpx.Client(timeout=timeout, headers=_HTTP_HEADERS) as c:
            r = c.get(DBPEDIA_SPARQL, params={"query": sparql})
            r.raise_for_status()
            return r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("DBpedia query failed: %s", exc)
        return {}


# ── Utilities ─────────────────────────────────────────────────────────────

def _slugify(label: str) -> str:
    """Stable apple: URI fragment from a person's display name."""
    return re.sub(r"[^A-Za-z0-9]+", "", label)


def _escape_literal(s: str) -> str:
    """Escape a string for safe inclusion in a SPARQL string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


# ── Step 1: pull canonical Apple persons from DBpedia ─────────────────────

# NOTE: We use the full IRI <http://dbpedia.org/resource/Apple_Inc.> instead
# of dbr:Apple_Inc. — Virtuoso (DBpedia's SPARQL engine) reads the trailing
# dot in PN_LOCAL as a triple terminator and 400s the query with a cryptic
# "syntax error at '.' before 'dbo:founder'". Angle-bracketed IRIs sidestep
# the PN_LOCAL parser entirely.
_FETCH_CANONICAL_PERSONS = """
PREFIX dbo:  <http://dbpedia.org/ontology/>
PREFIX dbp:  <http://dbpedia.org/property/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?person ?name ?role WHERE {
  {
    # DBpedia is inconsistent — `dbo:founder` (curated) returns nothing
    # for Apple, but `dbp:founders` (raw infobox, note the PLURAL) has
    # Jobs, Wozniak and Wayne. We accept both spellings.
    { <http://dbpedia.org/resource/Apple_Inc.> dbo:founder ?person }
    UNION
    { <http://dbpedia.org/resource/Apple_Inc.> dbp:founders ?person }
    BIND("Founder" AS ?role)
  } UNION {
    { <http://dbpedia.org/resource/Apple_Inc.> dbo:keyPerson ?person }
    UNION
    { <http://dbpedia.org/resource/Apple_Inc.> dbp:keyPeople ?person }
    BIND("Executive" AS ?role)
  }
  ?person rdfs:label ?name .
  # Exclude the empty/literal blank entries DBpedia leaves in infoboxes.
  FILTER(ISIRI(?person))
  FILTER(LANG(?name) = "en")
}
"""


def fetch_canonical_persons() -> list[dict]:
    """Returns a list of ``{person_uri, name, role}`` dicts from DBpedia.

    ``role`` is one of ``"Founder"`` or ``"Executive"`` (DBpedia
    ``dbo:keyPerson`` covers CEOs, chairmen, board members generically —
    we don't try to disambiguate further from this query alone)."""
    res = _dbpedia_query(_FETCH_CANONICAL_PERSONS)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for b in res.get("results", {}).get("bindings", []):
        key = (b["person"]["value"], b["role"]["value"])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "person_uri": b["person"]["value"],
            "name":       b["name"]["value"],
            "role":       b["role"]["value"],
        })
    return out


def _person_already_present(dbpedia_uri: str, role: str) -> bool:
    """True if some person in our graph already has this DBpedia
    sameAs AND the given role typed. Avoids duplicate inserts."""
    ask = f"""
PREFIX apple: <{APPLE_NS}>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
ASK {{
  ?p owl:sameAs <{dbpedia_uri}> ;
     a apple:{role} .
}}
"""
    try:
        res = graphdb_client.select(ask)
        return bool(res.get("boolean"))
    except Exception as exc:  # noqa: BLE001
        log.warning("ASK for %s failed: %s", dbpedia_uri, exc)
        return False


def insert_canonical_person(p: dict) -> bool:
    """Insert a canonical person into the graph if not already present.

    Returns True iff something was actually inserted."""
    role = p["role"]
    if role not in {"Founder", "Executive"}:
        log.warning("Skipping unexpected role %r for %s", role, p["name"])
        return False
    if _person_already_present(p["person_uri"], role):
        return False
    apple_uri = f"apple:{_slugify(p['name'])}"
    update = f"""
PREFIX apple: <{APPLE_NS}>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
PREFIX foaf:  <http://xmlns.com/foaf/0.1/>
INSERT DATA {{
  {apple_uri} a apple:{role} ;
              a apple:Person ;
              rdfs:label "{_escape_literal(p['name'])}"@en ;
              foaf:name  "{_escape_literal(p['name'])}" ;
              owl:sameAs <{p['person_uri']}> .
}}
"""
    graphdb_client.update(update)
    return True


# ── Step 2: verify existing persons are Apple-related ─────────────────────

_LIST_UNVERIFIED_PERSONS = """
PREFIX apple: <http://uc5.butscher.cloud/apple#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?p ?name WHERE {
  ?p a apple:Person ;
     rdfs:label ?name .
  FILTER NOT EXISTS { ?p a apple:CEO }
  FILTER NOT EXISTS { ?p a apple:Founder }
  FILTER NOT EXISTS { ?p a apple:Designer }
  FILTER NOT EXISTS { ?p a apple:Engineer }
  FILTER NOT EXISTS { ?p a apple:Executive }
  FILTER NOT EXISTS { ?p a apple:UnrelatedPerson }
}
"""


def list_unverified_persons() -> list[dict]:
    """All persons in the graph without an Apple-specific role typed and
    without an UnrelatedPerson demotion. These are the candidates for
    Step-2 DBpedia verification."""
    res = graphdb_client.select(_LIST_UNVERIFIED_PERSONS)
    return [
        {"uri": b["p"]["value"], "name": b["name"]["value"]}
        for b in res.get("results", {}).get("bindings", [])
    ]


def is_apple_related(name: str) -> bool:
    """ASK DBpedia: does any person with this exact English label have
    a direct relation to ``dbr:Apple_Inc.`` (employer / keyPerson /
    founder)?

    Returns False on transport error — better to leave the person alone
    than to wrongly demote them when DBpedia is down."""
    apple = "<http://dbpedia.org/resource/Apple_Inc.>"
    # Mirror the property variants from _FETCH_CANONICAL_PERSONS — Apple's
    # entries in DBpedia mostly use dbp:founders + dbp:keyPeople, the
    # curated dbo:founder/keyPerson barely have data. We also check
    # person→company direction (dbo:employer) for non-leadership employees.
    q = f"""
PREFIX dbo:  <http://dbpedia.org/ontology/>
PREFIX dbp:  <http://dbpedia.org/property/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
ASK WHERE {{
  ?person rdfs:label "{_escape_literal(name)}"@en .
  {{ ?person dbo:employer {apple} }}
  UNION
  {{ {apple} dbo:keyPerson ?person }}
  UNION
  {{ {apple} dbp:keyPeople ?person }}
  UNION
  {{ {apple} dbo:founder ?person }}
  UNION
  {{ {apple} dbp:founders ?person }}
}}
"""
    r = _dbpedia_query(q)
    return bool(r.get("boolean"))


def demote_to_unrelated(uri: str) -> None:
    """Mark a person as ``apple:UnrelatedPerson``. Keeps the node in the
    graph (their text-mentions might still be useful for UE1/UE3) but
    excludes them from Apple-specific role queries."""
    update = f"""
PREFIX apple: <{APPLE_NS}>
INSERT DATA {{
  <{uri}> a apple:UnrelatedPerson .
}}
"""
    graphdb_client.update(update)


# ── Orchestration ─────────────────────────────────────────────────────────

def validate_and_enrich(*, sleep_between_queries: float = 0.3) -> dict:
    """Run a full DBpedia validation + enrichment pass.

    ``sleep_between_queries`` throttles outbound DBpedia traffic — the
    public endpoint asks for at most a handful of hits per second per
    client, and our pass typically issues 20–40 ASKs.

    Returns a stats dict suitable for JSON serialisation back to a
    caller (API endpoint or CLI)."""
    log.info("DBpedia validation — step 1/2: fetch canonical Apple persons")
    canonical = fetch_canonical_persons()
    log.info("  DBpedia returned %d (person, role) pairs", len(canonical))
    added = 0
    for p in canonical:
        if insert_canonical_person(p):
            added += 1
            log.info("  + %s (%s)", p["name"], p["role"])
        time.sleep(sleep_between_queries)

    log.info("DBpedia validation — step 2/2: verify UE3-extracted persons")
    unverified = list_unverified_persons()
    log.info("  %d candidates without an Apple role", len(unverified))
    confirmed = 0
    demoted = 0
    for u in unverified:
        if is_apple_related(u["name"]):
            confirmed += 1
            log.info("  ✓ %s — Apple-related per DBpedia", u["name"])
        else:
            demote_to_unrelated(u["uri"])
            demoted += 1
            log.info("  − %s → UnrelatedPerson", u["name"])
        time.sleep(sleep_between_queries)

    stats = {
        "canonical_persons_fetched": len(canonical),
        "canonical_persons_added":   added,
        "unverified_persons_total":  len(unverified),
        "persons_confirmed":         confirmed,
        "persons_demoted":           demoted,
    }
    log.info("DBpedia validation done: %s", stats)
    return stats


if __name__ == "__main__":
    import json as _json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    print(_json.dumps(validate_and_enrich(), indent=2))
