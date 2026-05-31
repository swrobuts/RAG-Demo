"""Pull DBpedia cross-edges between entities we already have.

The UE3 graph is sparse — 40 nodes / 20 edges, ~0.5 edges per node, so
most ego networks are 1–2 nodes wide. This module asks DBpedia for any
properties that connect TWO existing UE3 entities (both with
``owl:sameAs`` links into DBpedia) and inserts them into Neo4j as
``RELATED_TO`` edges so they appear in /api/graph immediately.

Strategy
--------
1. Read every UE3 entity from Postgres that has a DBpedia sameAs in
   GraphDB (we already inserted these for products + persons).
2. Send one batched SPARQL to DBpedia with ``VALUES`` for both subjects
   AND objects = our known DBpedia URIs → returns every triple inside
   the closed set.
3. Translate the DBpedia predicate to an Apple-style relation name and
   INSERT into Neo4j.

Net effect: jeder Klick im Graph zeigt nicht mehr nur Apple plus 1–2
zufällige UE3-Treffer, sondern echte semantische Verbindungen wie
Steve Jobs → Apple (founder), Jonathan Ive → iPhone (designed),
iPhone 4 → iPhone 4S (successor).
"""
from __future__ import annotations

import logging
import re

import httpx

from backend.data.neo4j_client import neo4j_session

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

# DBpedia predicate → human-friendly relation name we use in Neo4j.
# Anything not in this map gets the catch-all "associated_with".
_PREDICATE_MAP = {
    # PERSON ↔ ORG / PRODUCT
    "http://dbpedia.org/ontology/employer":      "works_for",
    "http://dbpedia.org/ontology/founder":       "founded_by",
    "http://dbpedia.org/ontology/founders":      "founded_by",
    "http://dbpedia.org/property/founder":       "founded_by",
    "http://dbpedia.org/property/founders":      "founded_by",
    "http://dbpedia.org/ontology/keyPerson":     "key_person",
    "http://dbpedia.org/property/keyPeople":     "key_person",
    "http://dbpedia.org/ontology/designer":      "designed_by",
    "http://dbpedia.org/property/designer":      "designed_by",
    # PRODUCT ↔ PRODUCT
    "http://dbpedia.org/ontology/predecessor":   "predecessor_of",
    "http://dbpedia.org/ontology/successor":     "successor_of",
    "http://dbpedia.org/property/predecessor":   "predecessor_of",
    "http://dbpedia.org/property/successor":     "successor_of",
    # PRODUCT ↔ ORG
    "http://dbpedia.org/ontology/manufacturer":  "manufactured_by",
    "http://dbpedia.org/property/manufacturer":  "manufactured_by",
    "http://dbpedia.org/ontology/product":       "manufactures",
    "http://dbpedia.org/ontology/operatingSystem": "runs_on",
    # PERSON ↔ PRODUCT
    "http://dbpedia.org/ontology/knownFor":      "known_for",
    "http://dbpedia.org/property/knownFor":      "known_for",
    "http://dbpedia.org/property/notableWorks":  "known_for",
    # Generic
    "http://dbpedia.org/ontology/parent":        "parent_of",
    "http://dbpedia.org/ontology/subsidiary":    "subsidiary_of",
}


def _dbpedia_query(sparql: str, *, timeout: float = 45.0) -> dict:
    try:
        with httpx.Client(timeout=timeout, headers=_HTTP_HEADERS) as c:
            r = c.get(DBPEDIA_SPARQL, params={"query": sparql})
            r.raise_for_status()
            return r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("DBpedia query failed: %s", exc)
        return {}


def _fetch_entities_with_dbpedia() -> dict[str, str]:
    """Returns {dbpedia_uri: entity_key} for every UE3 entity whose
    apple:URI has an owl:sameAs to a DBpedia resource in GraphDB.

    The entity_key is the UE3 form ("PRODUCT:iphone", "PERSON:steve jobs"
    …) which is what Neo4j uses as node id."""
    from backend.data import graphdb_client
    from backend.ingest.ue4_ontology import _entity_uri
    from backend.data.pg import session_scope
    from sqlalchemy import text as sql_text

    # Step 1: every entity_key from Postgres
    with session_scope() as session:
        rows = session.execute(
            sql_text("SELECT entity_key, name FROM ue3.entity_summary")
        ).mappings().all()
    # Build apple_uri → entity_key map
    apple_to_key = {_entity_uri(r["entity_key"]): r["entity_key"] for r in rows}

    # Step 2: ask GraphDB for sameAs links
    apple_uris_filter = " ".join(f"<{u}>" for u in apple_to_key)
    if not apple_uris_filter:
        return {}
    q = f"""
PREFIX owl:   <http://www.w3.org/2002/07/owl#>
SELECT ?s ?o WHERE {{
  VALUES ?s {{ {apple_uris_filter} }}
  ?s owl:sameAs ?o .
  FILTER(STRSTARTS(STR(?o), "http://dbpedia.org/resource/"))
}}
"""
    try:
        res = graphdb_client.select(q)
    except Exception as exc:  # noqa: BLE001
        log.warning("GraphDB sameAs query failed: %s", exc)
        return {}
    dbr_to_key: dict[str, str] = {}
    for b in res.get("results", {}).get("bindings", []):
        apple_uri = b["s"]["value"]
        dbr_uri = b["o"]["value"]
        key = apple_to_key.get(apple_uri)
        if key:
            dbr_to_key[dbr_uri] = key
    return dbr_to_key


def fetch_cross_edges() -> list[dict]:
    """For every (s, o) pair both in our entity set, fetch every DBpedia
    triple ?s ?p ?o → translate to {src_key, tgt_key, rel_type}.
    Self-loops dropped. Duplicates by (src, tgt, rel) collapsed.
    """
    dbr_to_key = _fetch_entities_with_dbpedia()
    if not dbr_to_key:
        log.info("No entities with DBpedia sameAs links — nothing to fetch")
        return []
    uris_block = " ".join(f"<{u}>" for u in dbr_to_key)
    q = f"""
SELECT DISTINCT ?s ?p ?o WHERE {{
  VALUES ?s {{ {uris_block} }}
  VALUES ?o {{ {uris_block} }}
  ?s ?p ?o .
  FILTER(STRSTARTS(STR(?p), "http://dbpedia.org/"))
}}
"""
    res = _dbpedia_query(q)
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for b in res.get("results", {}).get("bindings", []):
        s_uri = b["s"]["value"]
        o_uri = b["o"]["value"]
        p_uri = b["p"]["value"]
        if s_uri == o_uri:
            continue
        src = dbr_to_key.get(s_uri)
        tgt = dbr_to_key.get(o_uri)
        if not src or not tgt:
            continue
        rel = _PREDICATE_MAP.get(p_uri, "associated_with")
        key = (src, tgt, rel)
        if key in seen:
            continue
        seen.add(key)
        out.append({"src": src, "tgt": tgt, "rel": rel, "predicate": p_uri})
    return out


def insert_cross_edges(edges: list[dict]) -> int:
    """Upsert edges into Neo4j as RELATED_TO with type=rel and weight=1.
    Returns number of fresh edges inserted (MERGE-on-create count)."""
    if not edges:
        return 0
    inserted = 0
    with neo4j_session() as session:
        for e in edges:
            r = session.run(
                """
                MATCH (a:Entity {id: $src}), (b:Entity {id: $tgt})
                MERGE (a)-[r:RELATED_TO {type: $rel}]->(b)
                ON CREATE SET r.weight = 1, r.source = 'dbpedia'
                ON MATCH SET r.weight = COALESCE(r.weight, 1)
                RETURN r
                """,
                src=e["src"], tgt=e["tgt"], rel=e["rel"],
            ).single()
            if r:
                inserted += 1
    return inserted


def enrich_edges() -> dict:
    """End-to-end: pull DBpedia cross-edges, MERGE into Neo4j.
    Stats are returned for the API caller."""
    log.info("DBpedia edges: fetching cross-edges …")
    edges = fetch_cross_edges()
    log.info("  DBpedia returned %d candidate edges", len(edges))
    inserted = insert_cross_edges(edges)
    # Count by relation type
    by_rel: dict[str, int] = {}
    for e in edges:
        by_rel[e["rel"]] = by_rel.get(e["rel"], 0) + 1
    stats = {
        "edges_fetched": len(edges),
        "edges_upserted": inserted,
        "by_relation": by_rel,
    }
    log.info("DBpedia edges done: %s", stats)
    return stats


if __name__ == "__main__":
    import json as _json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    print(_json.dumps(enrich_edges(), indent=2))
