"""Push canonical (GraphDB-only) entities also into UE3's storage layer.

Before this helper, DBpedia validator + products + canonical-persons TTL
only wrote into GraphDB. As a result they were invisible to:
  - UE3 GraphRAG retrieval (queries Postgres entity_summary + Neo4j)
  - The /api/graph visualisation in the old code (now augmented separately)

This module bridges that gap: for every canonical apple:* entity that
has rdfs:label and no UE3 entry yet, INSERT a row into
``ue3.entity_summary`` and a node in Neo4j with id=``CANON:<localname>``.

The descriptions are short (just the role names + DBpedia link) so the
answering LLM can still produce a coherent response when UE3's local
text retrieval hits these entities.
"""
from __future__ import annotations

import logging
import re
import unicodedata

from sqlalchemy import text as sql_text

from backend.data.neo4j_client import neo4j_session
from backend.data.pg import session_scope

log = logging.getLogger(__name__)

APPLE_NS = "http://uc5.butscher.cloud/apple#"
_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _local_name(uri: str) -> str:
    return uri.rsplit("#", 1)[-1]


# Mirror of the type mapping used by /api/graph
_TYPE_MAP = {
    "Person":       "PERSON",
    "Organization": "ORGANIZATION",
    "Company":      "ORGANIZATION",
    "Shareholder":  "ORGANIZATION",
    "Supplier":     "ORGANIZATION",
    "Product":      "PRODUCT",
    "HardwareProduct": "PRODUCT",
    "SoftwareProduct": "PRODUCT",
    "Smartphone":   "PRODUCT", "Tablet": "PRODUCT", "Wearable": "PRODUCT",
    "Computer":     "PRODUCT", "Desktop": "PRODUCT", "Notebook": "PRODUCT",
    "OperatingSystem": "PRODUCT", "OnlineService": "PRODUCT",
    "ProductFamily": "PRODUCT",
    "Event":        "EVENT",
    "Era":          "EVENT",
    "Location":     "LOCATION",
    "Concept":      "CONCEPT",
}


def fetch_canonical_with_roles() -> list[dict]:
    """Pull every canonical entity from GraphDB with its broadest type and
    the narrative roles. Returns list of dicts with uri/name/type/roles."""
    from backend.data import graphdb_client
    q = f"""
PREFIX apple: <{APPLE_NS}>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?s ?label (GROUP_CONCAT(DISTINCT ?clsName; separator="|") AS ?classes) WHERE {{
  ?s a ?cls ;
     rdfs:label ?label .
  FILTER(STRSTARTS(STR(?s),   "{APPLE_NS}"))
  FILTER(STRSTARTS(STR(?cls), "{APPLE_NS}"))
  FILTER(LANG(?label) = "en")
  BIND(STRAFTER(STR(?cls), "#") AS ?clsName)
}}
GROUP BY ?s ?label
"""
    try:
        res = graphdb_client.select(q)
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_canonical_with_roles: GraphDB query failed: %s", exc)
        return []
    out: list[dict] = []
    for b in res.get("results", {}).get("bindings", []):
        uri    = b["s"]["value"]
        label  = b["label"]["value"]
        classes = (b.get("classes", {}).get("value") or "").split("|")
        # Pick the broadest type from the chain
        broad = None
        for c in classes:
            if c in _TYPE_MAP:
                broad = _TYPE_MAP[c]
                break
        if not broad:
            continue
        out.append({
            "uri": uri, "label": label, "type": broad,
            "classes": [c for c in classes if c and c != "Person" and c != "Organization"
                        and c != "Product" and c != "Event" and c != "Location" and c != "Concept"],
        })
    return out


def push_to_ue3() -> dict:
    """For each canonical entity:
      1. If a row in ue3.entity_summary already covers it (by name match),
         skip. Otherwise INSERT with entity_key=CANON:<localname>.
      2. MERGE a Neo4j :Entity node with the same id so RELATED_TO edges
         we inserted via the enrichment modules find both ends.
    Idempotent, returns stats."""
    canonical = fetch_canonical_with_roles()
    if not canonical:
        return {"canonical_fetched": 0, "pg_inserted": 0, "neo4j_inserted": 0, "skipped": 0}

    # Existing entity names (lowercase) — for fuzzy skip
    with session_scope() as session:
        existing_names = {
            (r["name"] or "").strip().lower()
            for r in session.execute(
                sql_text("SELECT name FROM ue3.entity_summary")
            ).mappings().all()
        }

    pg_inserted = 0
    neo4j_inserted = 0
    skipped = 0
    with session_scope() as session:
        for e in canonical:
            name = e["label"].strip()
            if name.lower() in existing_names:
                skipped += 1
                continue
            local = _SAFE_RE.sub("", _local_name(e["uri"])) or "Unknown"
            entity_key = f"CANON:{local}"
            description = f"Kanonisch aus DBpedia ({', '.join(e['classes']) or e['type']})."
            try:
                session.execute(sql_text(
                    "INSERT INTO ue3.entity_summary "
                    "(entity_key, name, type, description, mention_count) "
                    "VALUES (:key, :name, :type, :desc, 1) "
                    "ON CONFLICT (entity_key) DO NOTHING"
                ), {"key": entity_key, "name": name, "type": e["type"], "desc": description})
                pg_inserted += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("PG insert for %s failed: %s", entity_key, exc)

    # Neo4j outside the SQLAlchemy session
    with neo4j_session() as session:
        for e in canonical:
            name = e["label"].strip()
            if name.lower() in existing_names:
                continue
            local = _SAFE_RE.sub("", _local_name(e["uri"])) or "Unknown"
            entity_key = f"CANON:{local}"
            try:
                session.run(
                    "MERGE (n:Entity {id: $id}) "
                    "ON CREATE SET n.name = $name, n.type = $type, n.source = 'canonical' "
                    "ON MATCH  SET n.name = $name",
                    id=entity_key, name=name, type=e["type"],
                )
                neo4j_inserted += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("Neo4j MERGE for %s failed: %s", entity_key, exc)

    return {
        "canonical_fetched": len(canonical),
        "pg_inserted":  pg_inserted,
        "neo4j_inserted": neo4j_inserted,
        "skipped": skipped,
    }


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    print(json.dumps(push_to_ue3(), indent=2))
