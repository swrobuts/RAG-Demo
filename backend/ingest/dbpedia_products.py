"""Pull Apple product chronology from DBpedia into the UE4 ontology.

The UE3 entity extractor produces 87 % `apple:associatedWith` relations
and almost zero specific Apple-ontology properties — so a question like
"Was ist das Vorgängerprodukt vom Apple PowerBook 165?" can't be
answered from the local graph. The specific properties (predecessorOf,
successorOf, designedBy, …) exist as OWL classes but are essentially
unpopulated.

This module enriches the graph by querying DBpedia, which has 33–40
Apple products with rich chronology data:

  20 entities with dbo:predecessor
  22 entities with dbo:successor

For each product we don't already have, we insert:

  apple:X  a apple:Product .
  apple:X  rdfs:label "..."@en .
  apple:X  owl:sameAs <dbr:...> .                  # provenance back-link
  apple:X  apple:successorOf apple:Y .             # when DBpedia has it
  apple:X  apple:predecessorOf apple:Y .

Idempotent — re-running is a no-op when the data is already there.
Doesn't touch the UE3-extracted entities (they keep their existing
apple:associatedWith relations).
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata

import httpx

from backend.data import graphdb_client

log = logging.getLogger(__name__)

DBPEDIA_SPARQL = "https://dbpedia.org/sparql"
APPLE_NS = "http://uc5.butscher.cloud/apple#"
APPLE_DBR = "<http://dbpedia.org/resource/Apple_Inc.>"
USER_AGENT = (
    "UC5-RAG-Apple/1.0 "
    "(educational demo; https://github.com/swrobuts/Dashboard_sample)"
)
_HTTP_HEADERS = {
    "Accept": "application/sparql-results+json",
    "User-Agent": USER_AGENT,
}


# ── HTTP ──────────────────────────────────────────────────────────────────

def _dbpedia_query(sparql: str, *, timeout: float = 30.0) -> dict:
    """Run SPARQL against public DBpedia. Returns {} on transport failure."""
    try:
        with httpx.Client(timeout=timeout, headers=_HTTP_HEADERS) as c:
            r = c.get(DBPEDIA_SPARQL, params={"query": sparql})
            r.raise_for_status()
            return r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("DBpedia query failed: %s", exc)
        return {}


# ── URI handling ──────────────────────────────────────────────────────────

_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _slugify(name: str) -> str:
    """Stable apple: local-name from a DBpedia label. Same recipe as
    backend.ingest.ue4_ontology._entity_uri so DBpedia-imported products
    use the same URI shape as UE3-extracted ones."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    words = [w for w in re.split(r"\s+", s) if w]
    pascal = "".join(w[:1].upper() + w[1:] for w in words)
    return _SAFE_RE.sub("", pascal) or "Unknown"


def _escape_literal(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


# ── DBpedia: fetch Apple products with chronology ─────────────────────────

# One pass query: for every Apple-manufactured product, also pull its
# predecessor + successor (when present) along with their English labels.
# OPTIONAL keeps products in the result even when chronology is missing.
_FETCH_PRODUCTS = f"""
PREFIX dbo:  <http://dbpedia.org/ontology/>
PREFIX dbp:  <http://dbpedia.org/property/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?product ?label ?predecessor ?predLabel ?successor ?succLabel WHERE {{
  {{ ?product dbo:manufacturer {APPLE_DBR} }} UNION
  {{ ?product dbp:manufacturer {APPLE_DBR} }}
  ?product rdfs:label ?label .
  FILTER(LANG(?label) = "en")
  OPTIONAL {{
    ?product dbo:predecessor ?predecessor .
    ?predecessor rdfs:label ?predLabel .
    FILTER(LANG(?predLabel) = "en")
  }}
  OPTIONAL {{
    ?product dbo:successor ?successor .
    ?successor rdfs:label ?succLabel .
    FILTER(LANG(?succLabel) = "en")
  }}
}}
"""


def fetch_apple_products() -> list[dict]:
    """Returns a list of product dicts:
        { uri: dbpedia URI,
          name: rdfs:label@en,
          predecessor: dbpedia URI | None,
          predecessor_name: str | None,
          successor: dbpedia URI | None,
          successor_name: str | None }
    DBpedia returns one row per (product, predecessor, successor) tuple;
    we coalesce so each product appears once with its first chronology.
    """
    res = _dbpedia_query(_FETCH_PRODUCTS)
    by_uri: dict[str, dict] = {}
    for b in res.get("results", {}).get("bindings", []):
        uri = b["product"]["value"]
        item = by_uri.setdefault(uri, {
            "uri": uri,
            "name": b["label"]["value"],
            "predecessor": None, "predecessor_name": None,
            "successor": None,   "successor_name": None,
        })
        if "predecessor" in b and item["predecessor"] is None:
            item["predecessor"] = b["predecessor"]["value"]
            item["predecessor_name"] = b["predLabel"]["value"]
        if "successor" in b and item["successor"] is None:
            item["successor"] = b["successor"]["value"]
            item["successor_name"] = b["succLabel"]["value"]
    return list(by_uri.values())


# ── GraphDB: insert / skip-if-present ─────────────────────────────────────

def _product_exists(apple_local: str) -> bool:
    """ASK whether a product with this apple: local name is already in
    the graph (either as an inserted product or from UE3 extraction)."""
    q = f"""
PREFIX apple: <{APPLE_NS}>
ASK {{ apple:{apple_local} ?p ?o }}
"""
    try:
        r = graphdb_client.select(q)
        return bool(r.get("boolean"))
    except Exception as exc:  # noqa: BLE001
        log.warning("ASK %s failed: %s", apple_local, exc)
        return False


def insert_product(p: dict) -> dict:
    """Insert one product + its chronology back-links. Returns counts of
    what was actually added (so the orchestrator can report)."""
    local = _slugify(p["name"])
    triples: list[str] = []
    added = {"product": 0, "successor": 0, "predecessor": 0}

    if not _product_exists(local):
        triples.append(
            f'  apple:{local} a apple:Product ;\n'
            f'                rdfs:label "{_escape_literal(p["name"])}"@en ;\n'
            f'                owl:sameAs <{p["uri"]}> .'
        )
        added["product"] = 1

    if p["successor"]:
        succ_local = _slugify(p["successor_name"])
        if not _product_exists(succ_local):
            triples.append(
                f'  apple:{succ_local} a apple:Product ;\n'
                f'                     rdfs:label "{_escape_literal(p["successor_name"])}"@en ;\n'
                f'                     owl:sameAs <{p["successor"]}> .'
            )
        # The relation (idempotent — SPARQL INSERT is a set semantics)
        triples.append(f"  apple:{local} apple:successorOf apple:{succ_local} .")
        added["successor"] = 1

    if p["predecessor"]:
        pred_local = _slugify(p["predecessor_name"])
        if not _product_exists(pred_local):
            triples.append(
                f'  apple:{pred_local} a apple:Product ;\n'
                f'                     rdfs:label "{_escape_literal(p["predecessor_name"])}"@en ;\n'
                f'                     owl:sameAs <{p["predecessor"]}> .'
            )
        triples.append(f"  apple:{local} apple:predecessorOf apple:{pred_local} .")
        added["predecessor"] = 1

    if not triples:
        return added

    update = (
        f"PREFIX apple: <{APPLE_NS}>\n"
        f"PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>\n"
        f"PREFIX owl:   <http://www.w3.org/2002/07/owl#>\n"
        f"INSERT DATA {{\n"
        + "\n".join(triples)
        + "\n}\n"
    )
    graphdb_client.update(update)
    return added


# ── Orchestration ─────────────────────────────────────────────────────────

def enrich_products(*, sleep_between: float = 0.2) -> dict:
    """Pull all DBpedia Apple products, insert any missing ones, attach
    their successor/predecessor chronology. Idempotent."""
    log.info("DBpedia products: fetching list …")
    products = fetch_apple_products()
    log.info("  DBpedia returned %d products", len(products))
    stats = {
        "products_fetched":      len(products),
        "products_added":        0,
        "successor_added":       0,
        "predecessor_added":     0,
    }
    for p in products:
        try:
            added = insert_product(p)
            stats["products_added"]    += added["product"]
            stats["successor_added"]   += added["successor"]
            stats["predecessor_added"] += added["predecessor"]
            if any(added.values()):
                log.info("  + %s (added: %s)", p["name"], added)
        except Exception as exc:  # noqa: BLE001
            log.warning("insert_product(%s) failed: %s", p["name"], exc)
        time.sleep(sleep_between)
    log.info("DBpedia products done: %s", stats)
    return stats


if __name__ == "__main__":
    import json as _json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    print(_json.dumps(enrich_products(), indent=2))
