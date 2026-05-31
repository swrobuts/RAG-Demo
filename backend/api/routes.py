from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from backend.api.schemas import (
    CompareRequest,
    CompareResponse,
    IngestRequest,
    QueryRequest,
    QueryResponse,
    SourcePayload,
    StrategyResult,
)
from backend.config import get_settings
from backend.data import repo
from backend.data.graphdb_client import ping as graphdb_ping
from backend.data.neo4j_client import ping as neo4j_ping
from backend.data.pg import ping as pg_ping
from backend.data.pg import session_scope
from backend.data.wikipedia_loader import fetch_article
from backend.evaluation.judge import judge_answers
from backend.ingest.canonical_to_ue3 import push_to_ue3
from backend.ingest.cooccurrence import enrich_cooccurrence
from backend.ingest.dbpedia_edges import enrich_edges
from backend.ingest.dbpedia_products import enrich_products
from backend.ingest.dbpedia_validator import validate_and_enrich
from backend.ingest.ue1_simple import run_ue1_ingest
from backend.ingest.ue2_pageindex import run_ue2_ingest
from backend.ingest.ue3_graphrag import run_ue3_ingest
from backend.ingest.ue4_ontology import run_ue4_ingest
from backend.llm.factory import get_chat_llm
from backend.retrieval.base import RetrievalResult
from backend.retrieval.graphrag import GraphRAG
from backend.retrieval.ontology import OntologyRAG
from backend.retrieval.pageindex import PageIndexRAG
from backend.retrieval.simple import SimpleRAG

log = logging.getLogger(__name__)
router = APIRouter()


# OWL sub-classes considered "narrative" — visible to the user as role
# tags. Everything else (apple:Person, apple:Organization etc. — the
# broad parents we already expose as `type`) is filtered out.
_NARRATIVE_ROLES = {
    # PERSON sub-classes
    "CEO", "Founder", "Designer", "Engineer", "Executive", "Employee",
    "UnrelatedPerson",
    # ORG sub-classes
    "Company", "Shareholder", "Supplier",
    # PRODUCT sub-classes
    "Smartphone", "Tablet", "Wearable", "Computer", "Desktop", "Notebook",
    "OperatingSystem", "OnlineService", "ProductFamily",
    # EVENT sub-classes
    "Era",
}


# Mapping OWL parent class → broad type used by /api/graph
_GRAPHDB_TYPE_MAP = {
    "Person":       "PERSON",
    "Organization": "ORGANIZATION",
    "Company":      "ORGANIZATION",
    "Product":      "PRODUCT",
    "HardwareProduct": "PRODUCT",
    "SoftwareProduct": "PRODUCT",
    "Event":        "EVENT",
    "Era":          "EVENT",
    "Location":     "LOCATION",
    "Concept":      "CONCEPT",
}


def _fetch_canonical_entities(exclude_keys: set[str]) -> list[dict]:
    """Pull entities that live ONLY in GraphDB (DBpedia-validator and
    products enrichments inserted them) so they appear in the graph
    visualisation too — not just in SPARQL results.

    Maps each apple:URI to a synthetic entity_key ``CANON:<localname>``
    and the broadest applicable type from _GRAPHDB_TYPE_MAP. Excludes
    anything whose synthesised key collides with the UE3 set or whose
    name (case-insensitive) already exists in UE3.
    """
    from backend.data import graphdb_client
    APPLE_NS = "http://uc5.butscher.cloud/apple#"
    q = f"""
PREFIX apple: <{APPLE_NS}>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
SELECT ?s ?label ?cls WHERE {{
  ?s a ?cls ;
     rdfs:label ?label .
  FILTER(STRSTARTS(STR(?s), "{APPLE_NS}"))
  FILTER(STRSTARTS(STR(?cls), "{APPLE_NS}"))
  FILTER(LANG(?label) = "en")
}}
"""
    try:
        res = graphdb_client.select(q)
    except Exception as exc:  # noqa: BLE001
        log.warning("_fetch_canonical_entities: GraphDB query failed: %s", exc)
        return []
    # Collect: for each subject, pick the broad type and the best label.
    by_uri: dict[str, dict] = {}
    for b in res.get("results", {}).get("bindings", []):
        uri    = b["s"]["value"]
        label  = b["label"]["value"]
        cls    = b["cls"]["value"].rsplit("#", 1)[-1]
        broad  = _GRAPHDB_TYPE_MAP.get(cls)
        item = by_uri.setdefault(uri, {
            "uri": uri, "label": label, "broad": None,
        })
        if broad and not item["broad"]:
            item["broad"] = broad
    out: list[dict] = []
    seen_names = {k.split(":", 1)[1].lower() for k in exclude_keys if ":" in k}
    for uri, e in by_uri.items():
        if not e["broad"]:
            continue
        local = uri.rsplit("#", 1)[-1]
        # Skip "UnrelatedPerson" + bookkeeping classes via UnrelatedPerson set
        # — they're typed apple:Person too but flagged as off-topic.
        if e["label"].lower() in seen_names:
            continue
        out.append({
            "entity_key":    f"CANON:{local}",
            "name":          e["label"],
            "type":          e["broad"],
            "description":   "",   # canonical seed doesn't carry abstracts here
            "mention_count": 1,
            "uri":           uri,
        })
    return out


def _fetch_canonical_relations(uri_to_key: dict[str, str]) -> list[dict]:
    """Pull predecessor/successor/founded/designedBy/wasCEOOf relations
    from GraphDB and translate to /api/graph edge dicts using uri_to_key.

    Both ends must be in uri_to_key — we don't insert phantom nodes."""
    from backend.data import graphdb_client
    APPLE_NS = "http://uc5.butscher.cloud/apple#"
    q = f"""
PREFIX apple: <{APPLE_NS}>
SELECT ?s ?p ?o WHERE {{
  ?s ?p ?o .
  FILTER(STRSTARTS(STR(?s), "{APPLE_NS}"))
  FILTER(STRSTARTS(STR(?o), "{APPLE_NS}"))
  FILTER(?p IN (
    apple:predecessorOf, apple:successorOf,
    apple:founded, apple:foundedBy,
    apple:designedBy, apple:designed,
    apple:wasCEOOf, apple:hasCEO,
    apple:worksFor, apple:hasEmployee,
    apple:manufactures, apple:manufacturedBy,
    apple:associatedWith
  ))
}}
"""
    try:
        res = graphdb_client.select(q)
    except Exception as exc:  # noqa: BLE001
        log.warning("_fetch_canonical_relations: GraphDB query failed: %s", exc)
        return []
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for b in res.get("results", {}).get("bindings", []):
        s, p, o = b["s"]["value"], b["p"]["value"], b["o"]["value"]
        if s == o: continue
        src_k = uri_to_key.get(s)
        tgt_k = uri_to_key.get(o)
        if not src_k or not tgt_k:
            continue
        rel = p.rsplit("#", 1)[-1]
        key = (src_k, tgt_k, rel)
        if key in seen: continue
        seen.add(key)
        out.append({"src": src_k, "tgt": tgt_k, "type": rel, "weight": 1})
    return out


def _fetch_entity_roles(entity_keys: list[str]) -> dict[str, list[str]]:
    """For each UE3 entity_key, return the list of OWL sub-class names
    asserted in GraphDB (e.g. "CEO", "Designer", "Smartphone").

    Implementation: one batched SPARQL pulling every (subject, class)
    pair restricted to apple: classes; then we group on the Python side
    and intersect with the curated narrative role set so we don't show
    bookkeeping classes like Person/Organization (already in `type`).

    Returns {} on GraphDB failure — UE4 not available shouldn't crash
    the graph endpoint, just degrades the panel.
    """
    if not entity_keys:
        return {}
    from backend.data import graphdb_client
    from backend.ingest.ue4_ontology import _entity_uri
    key_by_uri: dict[str, str] = {}
    for k in entity_keys:
        key_by_uri[_entity_uri(k)] = k
    q = """
PREFIX apple: <http://uc5.butscher.cloud/apple#>
PREFIX rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
SELECT ?s ?cls WHERE {
  ?s a ?cls .
  FILTER(STRSTARTS(STR(?s),   "http://uc5.butscher.cloud/apple#"))
  FILTER(STRSTARTS(STR(?cls), "http://uc5.butscher.cloud/apple#"))
}
"""
    try:
        res = graphdb_client.select(q)
    except Exception as exc:  # noqa: BLE001
        log.warning("_fetch_entity_roles: GraphDB query failed: %s", exc)
        return {}
    out: dict[str, list[str]] = {}
    for b in res.get("results", {}).get("bindings", []):
        subject_uri = b["s"]["value"]
        class_uri   = b["cls"]["value"]
        key = key_by_uri.get(subject_uri)
        if not key:
            continue
        class_name = class_uri.rsplit("#", 1)[-1]
        if class_name not in _NARRATIVE_ROLES:
            continue
        out.setdefault(key, [])
        if class_name not in out[key]:
            out[key].append(class_name)
    # Sort each role list for stable output (most-specific intuition
    # is hard from class names alone, so alphabetical is fine).
    for v in out.values():
        v.sort()
    return out


# ── Health & metadata ─────────────────────────────────────────────────────

@router.get("/graph")
def graph(
    min_mentions: int = 1,
    types: str | None = None,
    limit_entities: int = 250,
    min_edge_weight: int = 1,
) -> dict:
    """Return the UE3 knowledge graph as a JSON node-and-edge document for the
    frontend's force-directed visualisation.

    Filters:
      - ``min_mentions``: drop entities with fewer than this many MENTIONS edges.
      - ``types``: comma-separated whitelist (e.g. "PERSON,PRODUCT"); empty = all.
      - ``limit_entities``: hard cap so the browser doesn't choke on huge graphs.
      - ``min_edge_weight``: drop edges with weight below this. Lets the
        frontend hide weak co-occurrence noise while keeping strong
        relations (default 1 = keep everything; UI defaults to 3).
    """
    type_filter = None
    if types:
        type_filter = {t.strip().upper() for t in types.split(",") if t.strip()}

    # Pull entities + their community membership + mention count from Postgres
    # (faster than Neo4j for the metadata side).
    with session_scope() as session:
        ent_rows = session.execute(
            text(
                "SELECT entity_key, name, type, description, mention_count "
                "FROM ue3.entity_summary "
                "WHERE mention_count >= :m "
                "ORDER BY mention_count DESC "
                "LIMIT :lim"
            ),
            {"m": min_mentions, "lim": limit_entities},
        ).mappings().all()
        comm_rows = session.execute(
            text(
                "SELECT community_id, level, size, summary, entity_keys "
                "FROM ue3.community_summary"
            )
        ).mappings().all()

    if type_filter is not None:
        ent_rows = [r for r in ent_rows if r["type"] in type_filter]

    keep_keys = {r["entity_key"] for r in ent_rows}

    # Augment with canonical entities that live in GraphDB only
    # (DBpedia-validator + products enrichment + canonical-persons TTL).
    # Their entity_keys are CANON:<localname> so they don't collide.
    canonical = _fetch_canonical_entities(keep_keys)
    if type_filter is not None:
        canonical = [c for c in canonical if c["type"] in type_filter]
    # Filter by mention threshold too (canonical entries default to 1)
    canonical = [c for c in canonical if c["mention_count"] >= min_mentions]
    ent_rows = list(ent_rows) + [
        {"entity_key": c["entity_key"], "name": c["name"], "type": c["type"],
         "description": c["description"], "mention_count": c["mention_count"]}
        for c in canonical
    ]
    keep_keys = {r["entity_key"] for r in ent_rows}
    # uri_to_key map for canonical relations (UE3-extracted entities use
    # backend.ingest.ue4_ontology._entity_uri to compute their URI).
    from backend.ingest.ue4_ontology import _entity_uri
    uri_to_key: dict[str, str] = {}
    for r in ent_rows:
        if r["entity_key"].startswith("CANON:"):
            # canonical's URI is apple:<localname>
            local = r["entity_key"].split(":", 1)[1]
            uri_to_key[f"http://uc5.butscher.cloud/apple#{local}"] = r["entity_key"]
        else:
            uri_to_key[_entity_uri(r["entity_key"])] = r["entity_key"]

    if not keep_keys:
        return {"nodes": [], "edges": [], "communities": []}

    # Pull RELATED_TO edges between kept entities from Neo4j.
    from backend.data.neo4j_client import neo4j_session
    with neo4j_session() as session:
        rel_rows = session.run(
            "MATCH (a:Entity)-[r:RELATED_TO]->(b:Entity) "
            "WHERE a.id IN $keys AND b.id IN $keys "
            "  AND COALESCE(r.weight, 1) >= $min_w "
            "RETURN a.id AS src, b.id AS tgt, r.type AS type, r.weight AS weight",
            keys=list(keep_keys), min_w=min_edge_weight,
        ).data()
    # Augment with GraphDB apple:* relations between visible entities.
    # Covers predecessorOf / successorOf / founded / designedBy /
    # wasCEOOf / worksFor / manufactures + the catch-all associatedWith.
    rel_rows = list(rel_rows) + _fetch_canonical_relations(uri_to_key)

    # Build community lookup: entity_key → community_id (first containing community).
    entity_to_community: dict[str, str] = {}
    for c in comm_rows:
        for k in (c["entity_keys"] or []):
            entity_to_community.setdefault(k, c["community_id"])

    # Enrich nodes with OWL subclass roles from GraphDB (Designer, CEO,
    # Founder, Smartphone, …). Empty list if GraphDB unreachable.
    entity_roles = _fetch_entity_roles([r["entity_key"] for r in ent_rows])

    nodes = [
        {
            "id": r["entity_key"],
            "name": r["name"],
            "type": r["type"],
            "description": r["description"],
            "mentions": int(r["mention_count"]),
            "community_id": entity_to_community.get(r["entity_key"]),
            "roles": entity_roles.get(r["entity_key"], []),
        }
        for r in ent_rows
    ]
    edges = [
        {
            "source": row["src"],
            "target": row["tgt"],
            "type": row["type"],
            "weight": int(row.get("weight") or 1),
        }
        for row in rel_rows
    ]
    communities = [
        {
            "id": c["community_id"],
            "level": int(c["level"]),
            "size": int(c["size"]),
            "summary": c["summary"],
        }
        for c in comm_rows
    ]
    return {"nodes": nodes, "edges": edges, "communities": communities}


_SPARQL_UPDATE_RE = __import__("re").compile(
    r"^\s*(PREFIX[^\n]*\n\s*)*\s*(INSERT|DELETE|CLEAR|LOAD|CREATE|DROP|COPY|MOVE|ADD)\b",
    __import__("re").IGNORECASE,
)


@router.post("/sparql/translate")
def sparql_translate(body: dict) -> dict:
    """Translate a natural-language question into SPARQL using the same
    LLM pipeline UE4 uses internally, but without executing it. The
    frontend's SPARQL console pre-fills the editor with the generated
    query so the user can review/edit before hitting Execute."""
    q = (body.get("query") or "").strip()
    if not q:
        raise HTTPException(400, "missing 'query' field")
    llm = body.get("llm") or "gemini"
    rag = OntologyRAG(llm_provider=llm)
    try:
        sparql = rag._generate_sparql(q)
        return {"ok": True, "sparql": sparql, "llm": llm}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:500]}


@router.post("/sparql")
def sparql(body: dict) -> dict:
    """Debug/teaching endpoint: send a raw SPARQL query OR update against
    the UE4 GraphDB repo and get the JSON results back. Bypasses the LLM.
    Routes INSERT/DELETE/CLEAR/LOAD to /statements (UPDATE endpoint),
    everything else to the query endpoint."""
    from backend.data import graphdb_client
    q = (body.get("query") or "").strip()
    if not q:
        raise HTTPException(400, "missing 'query' field")
    is_update = bool(_SPARQL_UPDATE_RE.match(q))
    try:
        if is_update:
            graphdb_client.update(q)
            return {"ok": True, "kind": "update", "result": "executed"}
        return {"ok": True, "kind": "query", "result": graphdb_client.select(q)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:500], "kind": "update" if is_update else "query"}


@router.post("/ue3/sync-canonical")
def ue3_sync_canonical() -> dict:
    """Push every canonical GraphDB-only entity (DBpedia-validator +
    products + canonical-persons TTL) into Postgres ue3.entity_summary
    AND Neo4j Entity nodes. Lets UE3 GraphRAG retrieval find them too.
    Idempotent; safe to re-run."""
    try:
        return {"ok": True, "stats": push_to_ue3()}
    except Exception as exc:  # noqa: BLE001
        log.exception("UE3 canonical sync failed")
        return {"ok": False, "error": str(exc)[:500]}


@router.post("/ue4/enrich-edges")
def ue4_enrich_edges() -> dict:
    """Pull DBpedia cross-edges between entities we already have.
    Inserts as Neo4j RELATED_TO edges so they appear in /api/graph."""
    try:
        return {"ok": True, "stats": enrich_edges()}
    except Exception as exc:  # noqa: BLE001
        log.exception("DBpedia edge enrichment failed")
        return {"ok": False, "error": str(exc)[:500]}


@router.post("/ue3/enrich-cooccurrence")
def ue3_enrich_cooccurrence() -> dict:
    """Add edges between entities mentioned in the same Wikipedia
    section. No external API — pure Postgres → Neo4j transform."""
    try:
        return {"ok": True, "stats": enrich_cooccurrence()}
    except Exception as exc:  # noqa: BLE001
        log.exception("Co-occurrence enrichment failed")
        return {"ok": False, "error": str(exc)[:500]}


@router.post("/ue4/enrich-products")
def ue4_enrich_products() -> dict:
    """Pull Apple product chronology (predecessor/successor) from DBpedia
    into the local GraphDB. Idempotent — re-running re-uploads the same
    triples (GraphDB deduplicates).

    Adds 40+ Apple products + ~40 chronology relations to the graph in
    one shot. Closes the UE3 data gap: queries like "Was ist der
    Vorgänger des iPhone 4?" suddenly work via SPARQL on real triples
    instead of falling back to text retrieval."""
    try:
        stats = enrich_products()
        return {"ok": True, "stats": stats}
    except Exception as exc:  # noqa: BLE001
        log.exception("DBpedia product enrichment failed")
        return {"ok": False, "error": str(exc)[:500]}


@router.post("/ue4/validate")
def ue4_validate() -> dict:
    """Cross-check the UE4 graph against DBpedia.

    Pulls canonical Apple persons (founders, key personnel) from DBpedia,
    inserts any we're missing, then verifies every UE3-extracted person
    has a real Apple connection — demotes context-only mentions to
    apple:UnrelatedPerson so role-queries stay clean.

    Idempotent. Takes ~10–30 s depending on how many unverified persons
    are in the graph and DBpedia's current latency."""
    try:
        stats = validate_and_enrich()
        return {"ok": True, "stats": stats}
    except Exception as exc:  # noqa: BLE001
        log.exception("DBpedia validation failed")
        return {"ok": False, "error": str(exc)[:500]}


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "db_ok": pg_ping(),
        "neo4j_ok": neo4j_ping(),
        "graphdb_ok": graphdb_ping(),
        "gemini_configured": bool(settings.gemini_api_key),
        "local_llm_url": settings.local_llm_url,
        "wikipedia_url": settings.wikipedia_url,
    }


@router.get("/snapshot")
def snapshot() -> dict:
    settings = get_settings()
    with session_scope() as session:
        snap = repo.latest_snapshot(session, settings.wikipedia_url)
    if snap is None:
        return {"snapshot": None}
    # Make timestamps JSON-friendly.
    snap["fetched_at"] = snap["fetched_at"].isoformat() if snap.get("fetched_at") else None
    return {"snapshot": snap}


@router.get("/strategies")
def strategies() -> dict:
    implemented = {"ue1", "ue2", "ue3", "ue4"}
    out = {}
    for s in ("ue1", "ue2", "ue3", "ue4"):
        with session_scope() as session:
            run = repo.latest_ingest_run(session, s)
            if s == "ue1":
                count = repo.ue1_chunk_count(session)
            elif s == "ue2":
                count = repo.ue2_tree_node_count(session)
            elif s == "ue3":
                count = repo.ue3_entity_count(session)
            else:  # ue4 — triples in GraphDB (cheap to query)
                try:
                    count = graphdb_triple_count()
                except Exception:  # noqa: BLE001
                    count = 0
        if run and run.get("started_at"):
            run["started_at"] = run["started_at"].isoformat()
        if run and run.get("finished_at"):
            run["finished_at"] = run["finished_at"].isoformat()
        out[s] = {
            "ingested": (run is not None and run.get("status") == "ok"),
            "implemented": s in implemented,
            "chunk_count": count,   # chunks(ue1) | nodes(ue2) | entities(ue3) | triples(ue4)
            "last_run": run,
        }
    return {"strategies": out}


def graphdb_triple_count() -> int:
    from backend.data.graphdb_client import count_triples
    return count_triples()


# ── Ingest ─────────────────────────────────────────────────────────────────

_ingest_lock = asyncio.Lock()


def _run_ingest_with_record(strategy: str, run_id: int, force: bool) -> None:
    """Worker: run the requested strategy's ingest and update meta.ingest_run."""
    try:
        if strategy == "ue1":
            s = run_ue1_ingest(force=force)
            stats = {
                "snapshot_created": s.snapshot_created,
                "document_id": s.document_id,
                "sections": s.sections,
                "chunks": s.chunks,
                "duration_ms": s.duration_ms,
            }
        elif strategy == "ue2":
            s = run_ue2_ingest(force=force)
            stats = {
                "document_id": s.document_id,
                "nodes": s.nodes,
                "leaves": s.leaves,
                "internal": s.internal,
                "llm_calls": s.llm_calls,
                "duration_ms": s.duration_ms,
            }
        elif strategy == "ue3":
            s = run_ue3_ingest(force=force)
            stats = {
                "chunks_processed": s.chunks_processed,
                "entities_unique": s.entities_unique,
                "relations_unique": s.relations_unique,
                "communities": s.communities,
                "llm_calls": s.llm_calls,
                "duration_ms": s.duration_ms,
            }
        elif strategy == "ue4":
            s = run_ue4_ingest(force=force)
            stats = {
                "entities_typed": s.entities_typed,
                "relations_inserted": s.relations_inserted,
                "sameas_links": s.sameas_links,
                "llm_calls": s.llm_calls,
                "triples_after": s.triples_after,
                "duration_ms": s.duration_ms,
            }
        else:
            raise ValueError(f"Strategy {strategy!r} not implemented")
        with session_scope() as session:
            repo.finish_ingest_run(session, run_id, status="ok", stats=stats)
    except Exception as exc:  # noqa: BLE001
        log.exception("%s ingest failed", strategy)
        with session_scope() as session:
            repo.finish_ingest_run(session, run_id, status="failed", error=str(exc))


@router.post("/ingest")
async def ingest(req: IngestRequest) -> dict:
    if req.strategy not in ("ue1", "ue2", "ue3", "ue4"):
        raise HTTPException(status_code=400, detail=f"Strategy {req.strategy} not implemented yet")
    if _ingest_lock.locked():
        raise HTTPException(status_code=409, detail="Another ingest is already running")

    settings = get_settings()
    # Synchronously fetch + create a snapshot row + start a run record so the
    # caller gets a run_id immediately.
    fetched = await asyncio.to_thread(fetch_article, settings.wikipedia_url)
    with session_scope() as session:
        snapshot_id, _ = repo.insert_or_get_snapshot(
            session,
            url=fetched.url,
            html=fetched.html,
            content_hash=fetched.content_hash,
            revision_id=fetched.revision_id,
            etag=fetched.etag,
        )
        run_id = repo.start_ingest_run(session, strategy=req.strategy, snapshot_id=snapshot_id)

    async def _run_locked() -> None:
        async with _ingest_lock:
            await asyncio.to_thread(_run_ingest_with_record, req.strategy, run_id, req.force)

    asyncio.create_task(_run_locked())
    return {"status": "started", "strategy": req.strategy, "run_id": run_id, "snapshot_id": snapshot_id}


@router.get("/ingest/{run_id}")
def ingest_status(run_id: int) -> dict:
    with session_scope() as session:
        row = repo.ingest_run(session, run_id)
    if row is None:
        raise HTTPException(404, "Run not found")
    if row.get("started_at"):
        row["started_at"] = row["started_at"].isoformat()
    if row.get("finished_at"):
        row["finished_at"] = row["finished_at"].isoformat()
    return row


# ── Query ──────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "Du bist ein präziser Assistent, der ausschließlich auf Basis der "
    "bereitgestellten Auszüge aus dem deutschen Wikipedia-Artikel zu *Apple* "
    "antwortet. Wenn die Auszüge die Frage nicht beantworten, sage das ehrlich. "
    "Zitiere die Sektion in Klammern, z. B. (Geschichte > Gründung)."
)


def _build_user_prompt(query: str, result: RetrievalResult) -> str:
    parts = ["# Frage", query, "", "# Auszüge"]
    for i, ch in enumerate(result.chunks, start=1):
        header = f"## Auszug {i}"
        if ch.section_path:
            header += f" — {ch.section_path}"
        parts.append(header)
        parts.append(ch.text.strip())
        parts.append("")
    parts.append("# Aufgabe")
    parts.append(
        "Beantworte die Frage knapp und korrekt. Stütze jede Aussage auf die "
        "Auszüge oben und nenne in Klammern die Sektion."
    )
    return "\n".join(parts)


def _get_strategy(name: str, llm: str):
    if name == "ue1":
        return SimpleRAG(llm_provider=llm)
    if name == "ue2":
        return PageIndexRAG(llm_provider=llm)
    if name == "ue3":
        return GraphRAG(llm_provider=llm)
    if name == "ue4":
        return OntologyRAG(llm_provider=llm)
    raise HTTPException(400, f"Strategy {name!r} not implemented yet")


def _run_strategy_and_answer(
    *, strategy: str, query: str, llm: str, k: int
) -> StrategyResult:
    """Shared by /query and /compare: retrieve + generate answer + collect metrics."""
    t0 = time.perf_counter()
    strat = _get_strategy(strategy, llm)
    result = strat.retrieve(query, k=k)
    if not result.chunks:
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return StrategyResult(
            strategy=strategy,
            answer=NO_CONTEXT_ANSWER,
            sources=[],
            trace={**result.trace, "skipped_llm": True, "reason": "no chunks retrieved"},
            latency_ms=latency_ms,
            llm_calls=int(result.trace.get("llm_calls_nav", 0)),
            token_usage={"prompt_tokens": 0, "completion_tokens": 0},
            skipped_llm=True,
        )
    chat = get_chat_llm(llm)
    user_prompt = _build_user_prompt(query, result)
    answer, usage = chat.generate(SYSTEM_PROMPT, user_prompt)
    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    return StrategyResult(
        strategy=strategy,
        answer=answer,
        sources=[SourcePayload(**asdict(s)) for s in result.sources],
        trace=result.trace,
        latency_ms=latency_ms,
        llm_calls=int(result.trace.get("llm_calls_nav", 0)) + 1,
        token_usage={
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
        },
        skipped_llm=False,
    )


NO_CONTEXT_ANSWER = (
    "Ich kann diese Frage nicht beantworten, weil die ausgewählte "
    "Retrieval-Strategie zu dieser Frage keine Auszüge aus dem Wikipedia-"
    "Artikel gefunden hat. Mögliche Ursachen: die Navigation hat die "
    "relevante Sektion verfehlt, oder die gewählten Sektionen enthalten "
    "keinen Text. Probiere die Frage mit einer anderen Strategie oder "
    "formuliere sie spezifischer."
)


@router.post("/query", response_model=QueryResponse)
def query(req: QueryRequest) -> QueryResponse:
    result = _run_strategy_and_answer(
        strategy=req.strategy, query=req.query, llm=req.llm, k=req.k or 8,
    )
    return QueryResponse(
        answer=result.answer,
        sources=result.sources,
        trace={**result.trace, "token_usage": result.token_usage},
        llm=req.llm,
        strategy=req.strategy,
        latency_ms=result.latency_ms,
    )


# ── Compare ────────────────────────────────────────────────────────────────

@router.post("/compare", response_model=CompareResponse)
async def compare(req: CompareRequest) -> CompareResponse:
    if not req.strategies:
        raise HTTPException(400, "At least one strategy must be selected")
    # Deduplicate while preserving order
    seen: set[str] = set()
    strategies = [s for s in req.strategies if not (s in seen or seen.add(s))]
    for s in strategies:
        if s not in ("ue1", "ue2", "ue3", "ue4"):
            raise HTTPException(400, f"Strategy {s!r} not implemented yet")

    t0 = time.perf_counter()

    # Run all strategies in parallel — each retrieves + generates its own
    # answer. asyncio.gather + to_thread keeps the event loop free during the
    # blocking LLM/DB calls.
    coros = [
        asyncio.to_thread(
            _run_strategy_and_answer,
            strategy=s,
            query=req.query,
            llm=req.llm,
            k=req.k or 8,
        )
        for s in strategies
    ]
    results: list[StrategyResult] = await asyncio.gather(*coros)

    # Judge runs once all strategies are done; uses Gemini for stable JSON.
    judge_payload = [
        {
            "strategy": r.strategy,
            "answer": r.answer,
            "sources": [s.model_dump() for s in r.sources],
        }
        for r in results
    ]
    evaluation = await asyncio.to_thread(judge_answers, req.query, judge_payload)
    total_latency_ms = round((time.perf_counter() - t0) * 1000, 1)

    return CompareResponse(
        query=req.query,
        llm=req.llm,
        results=results,
        evaluation=evaluation.to_dict(),
        total_latency_ms=total_latency_ms,
    )


@router.post("/query/stream")
def query_stream(req: QueryRequest):
    strategy = _get_strategy(req.strategy, req.llm)
    result = strategy.retrieve(req.query, k=req.k or 8)

    def gen_empty():
        meta = {
            "type": "meta",
            "sources": [],
            "trace": {**result.trace, "skipped_llm": True, "reason": "no chunks retrieved"},
            "strategy": req.strategy,
            "llm": req.llm,
        }
        yield f"data: {json.dumps(meta, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'token', 'text': NO_CONTEXT_ANSWER}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    if not result.chunks:
        return StreamingResponse(gen_empty(), media_type="text/event-stream")

    chat = get_chat_llm(req.llm)
    user_prompt = _build_user_prompt(req.query, result)

    def gen():
        meta = {
            "type": "meta",
            "sources": [asdict(s) for s in result.sources],
            "trace": result.trace,
            "strategy": req.strategy,
            "llm": req.llm,
        }
        yield f"data: {json.dumps(meta, ensure_ascii=False)}\n\n"
        for piece in chat.stream(SYSTEM_PROMPT, user_prompt):
            yield f"data: {json.dumps({'type': 'token', 'text': piece}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
