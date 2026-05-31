"""UE4 ingest: convert the UE3 graph data into OWL-aligned RDF triples in
GraphDB.

We piggy-back on UE3's extraction:
- UE3.entity_summary has 250+ entities with descriptions and types.
- UE3 Neo4j graph has the RELATED_TO co-occurrence edges.

For each entity we ask Gemini to produce SPARQL INSERT statements that
- typing the entity against our ontology (e.g. ``apple:CEO``),
- adding rdfs:label and apple:role,
- if confident, adding owl:sameAs to the DBpedia resource.

Edges from Neo4j are mapped to ontology properties via a second LLM pass
that takes the raw RELATED_TO type string ("FOUNDED", "WORKED_AT", …) plus
the typed endpoints, and picks the best-matching apple:* property — or
falls back to apple:associatedWith.

Everything lands as triples in the GraphDB repository; reasoning runs
automatically there.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass

from sqlalchemy import text

from backend.config import get_settings
from backend.data import graphdb_client
from backend.data.neo4j_client import neo4j_session
from backend.data.pg import session_scope
from backend.llm.factory import get_chat_llm

log = logging.getLogger(__name__)

APPLE_NS = "http://uc5.butscher.cloud/apple#"
APPLE_INC_URI = f"{APPLE_NS}AppleInc"

# Map UE3's coarse type → ontology class for fallback typing.
COARSE_TYPE_MAP = {
    "PERSON": "apple:Person",
    "ORGANIZATION": "apple:Organization",
    "PRODUCT": "apple:Product",
    "EVENT": "apple:Event",
    "LOCATION": "apple:Location",
    "CONCEPT": "apple:Concept",
}

# Property mapping: UE3 RELATED_TO.type → ontology property.
# This is "lite" — anything not listed maps to apple:associatedWith via LLM
# fallback later.
PROPERTY_MAP = {
    "FOUNDED": "apple:founded",
    "FOUNDED_BY": "apple:foundedBy",
    "GRUENDET": "apple:founded",
    "GEGRUENDET_VON": "apple:foundedBy",
    "WORKED_AT": "apple:worksFor",
    "ARBEITET_FUER": "apple:worksFor",
    "CEO_OF": "apple:wasCEOOf",
    "WAS_CEO": "apple:wasCEOOf",
    "HAS_CEO": "apple:hasCEO",
    "DESIGNED_BY": "apple:designedBy",
    "ENTWORFEN_VON": "apple:designedBy",
    "MANUFACTURES": "apple:manufactures",
    "MANUFACTURED_BY": "apple:manufacturedBy",
    "STELLT_HER": "apple:manufactures",
    "ENTWICKELT": "apple:designedBy",
    "SUCCESSOR_OF": "apple:successorOf",
    "PREDECESSOR_OF": "apple:predecessorOf",
    "NACHFOLGER_VON": "apple:successorOf",
    "VORGAENGER_VON": "apple:predecessorOf",
    "PART_OF": "apple:partOfFamily",
    "GEHOERT_ZU": "apple:partOfFamily",
}


@dataclass
class UE4IngestStats:
    entities_typed: int
    relations_inserted: int
    sameas_links: int
    llm_calls: int
    triples_after: int
    duration_ms: float


# ── URI generation ────────────────────────────────────────────────────────

_SAFE_CHAR_RE = re.compile(r"[^A-Za-z0-9_-]")


def _entity_uri(entity_key: str) -> str:
    """Derive a stable URI from a UE3 entity_key like 'PERSON:steve jobs'."""
    if ":" in entity_key:
        _type, name = entity_key.split(":", 1)
    else:
        name = entity_key
    # Strip diacritics + non-safe chars; capitalise first letter of each word
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    words = [w for w in re.split(r"\s+", s) if w]
    pascal = "".join(w[:1].upper() + w[1:] for w in words)
    pascal = _SAFE_CHAR_RE.sub("", pascal) or "Unknown"
    return f"{APPLE_NS}{pascal}"


# ── DBpedia probing ───────────────────────────────────────────────────────
# Light-touch: ask the LLM to guess the DBpedia URI from name+type+description.
# A confident "yes/no" gate prevents noise. We don't go online to DBpedia in
# this iteration — would add lookup latency and could be added later.

DBPEDIA_SAMEAS_SYSTEM = (
    "Du entscheidest, ob eine extrahierte Entität ein direktes DBpedia-Pendant "
    "hat, das vermutlich existiert (also kein Sondernamen, sondern eine "
    "verbreitete Person/Firma/Produkt). Antworte nur mit JSON: "
    '{\"dbpedia\": \"http://dbpedia.org/resource/...\" | null, \"confidence\": 0-1}'
)

DBPEDIA_SAMEAS_TEMPLATE = """Name: {name}
Typ: {type}
Beschreibung: {description}

Welcher DBpedia-Ressourcen-URI passt? Nur antworten wenn du dir bei
confidence >=0.8 sicher bist (z.B. Steve Jobs → http://dbpedia.org/resource/Steve_Jobs).
Sonst null. Nur JSON."""

ENTITY_TYPING_SYSTEM = (
    "Du wählst die passendste OWL-Klasse für eine extrahierte Entität aus "
    "der vorgegebenen Apple-Ontologie. Antworte mit JSON: "
    '{"class": "apple:..." } — exakt ein Klassen-Name aus der Liste.'
)

ENTITY_TYPING_TEMPLATE = """Name: {name}
Beschreibung: {description}
Grober Typ: {coarse_type}

Mögliche feinere Klassen (alle apple:*):
{classes}

Wähle die passendste. Wenn unsicher, nimm die grobe Klasse (apple:{coarse_type_cap}).
Nur JSON."""


# Subclasses we let the LLM pick from per coarse type.
FINER_CLASSES = {
    "PERSON": ["apple:Person", "apple:Employee", "apple:Executive",
                "apple:CEO", "apple:Founder", "apple:Designer", "apple:Engineer"],
    "ORGANIZATION": ["apple:Organization", "apple:Company",
                       "apple:Shareholder", "apple:Supplier"],
    "PRODUCT": ["apple:Product", "apple:HardwareProduct",
                  "apple:SoftwareProduct", "apple:OperatingSystem",
                  "apple:Computer", "apple:Desktop", "apple:Notebook",
                  "apple:MobileDevice", "apple:Smartphone", "apple:Tablet",
                  "apple:Wearable", "apple:OnlineService", "apple:ProductFamily"],
    "EVENT": ["apple:Event", "apple:Era"],
    "LOCATION": ["apple:Location"],
    "CONCEPT": ["apple:Concept"],
}


# ── parsing helpers ───────────────────────────────────────────────────────

import json
import concurrent.futures

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="ue4")


def _safe_json(raw: str) -> dict | None:
    if not raw:
        return None
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    decoder = json.JSONDecoder()
    for _ in range(3):
        idx = cleaned.find("{")
        if idx < 0:
            return None
        try:
            obj, _e = decoder.raw_decode(cleaned[idx:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        cleaned = cleaned[idx + 1:]
    return None


def _gen_with_timeout(chat, system: str, user: str, timeout: float = 30.0) -> str:
    fut = _executor.submit(chat.generate, system, user)
    try:
        raw, _u = fut.result(timeout=timeout)
        return raw or ""
    except concurrent.futures.TimeoutError as exc:
        fut.cancel()
        raise TimeoutError(f"Gemini exceeded {timeout:.0f}s") from exc


# ── data sources ──────────────────────────────────────────────────────────

@dataclass
class _Entity:
    key: str
    name: str
    coarse_type: str
    description: str
    mention_count: int


def _load_entities() -> list[_Entity]:
    with session_scope() as session:
        rows = session.execute(
            text(
                "SELECT entity_key, name, type, description, mention_count "
                "FROM ue3.entity_summary ORDER BY mention_count DESC"
            )
        ).mappings().all()
    return [
        _Entity(
            key=r["entity_key"], name=r["name"], coarse_type=r["type"],
            description=r["description"] or "",
            mention_count=int(r["mention_count"] or 0),
        )
        for r in rows
    ]


@dataclass
class _Relation:
    src_key: str
    src_type: str
    tgt_key: str
    tgt_type: str
    rel_type: str
    weight: int


def _load_relations() -> list[_Relation]:
    with neo4j_session() as session:
        rows = session.run(
            "MATCH (a:Entity)-[r:RELATED_TO]->(b:Entity) "
            "RETURN a.id AS sk, a.type AS st, b.id AS tk, b.type AS tt, "
            "       r.type AS rt, r.weight AS w"
        ).data()
    return [
        _Relation(
            src_key=row["sk"], src_type=row["st"],
            tgt_key=row["tk"], tgt_type=row["tt"],
            rel_type=row["rt"], weight=int(row.get("w") or 1),
        )
        for row in rows
    ]


# ── ingest steps ──────────────────────────────────────────────────────────

def _sparql_literal(s: str) -> str:
    """Escape a string for use as a SPARQL string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _build_entity_triples(
    entity: _Entity,
    ontology_class: str,
    dbpedia_uri: str | None,
) -> str:
    """Turtle/SPARQL-INSERT fragment for one entity."""
    uri = _entity_uri(entity.key)
    lines = [
        f"<{uri}> a {ontology_class} ;",
        f"  rdfs:label {_sparql_literal(entity.name)}@de ;",
        f"  foaf:name {_sparql_literal(entity.name)} ;",
    ]
    if entity.description:
        lines.append(f"  rdfs:comment {_sparql_literal(entity.description[:500])}@de ;")
    if dbpedia_uri:
        lines.append(f"  owl:sameAs <{dbpedia_uri}> ;")
    # Add an apple:role free-text field for searchable role information
    if entity.coarse_type == "PERSON":
        lines.append(f"  apple:role {_sparql_literal(entity.description[:200])} ;")
    # Replace trailing ; with .
    if lines[-1].endswith(";"):
        lines[-1] = lines[-1][:-1].rstrip() + " ."
    else:
        lines[-1] = lines[-1] + " ."
    return "\n".join(lines)


def _map_property(rel_type: str) -> str:
    """Map UE3 relation type to ontology property; fallback to associatedWith."""
    rt = rel_type.upper().replace(" ", "_") if rel_type else ""
    return PROPERTY_MAP.get(rt, "apple:associatedWith")


def _build_relation_triple(rel: _Relation) -> str:
    prop = _map_property(rel.rel_type)
    src_uri = _entity_uri(rel.src_key)
    tgt_uri = _entity_uri(rel.tgt_key)
    return f"<{src_uri}> {prop} <{tgt_uri}> ."


PREFIXES = """PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX foaf: <http://xmlns.com/foaf/0.1/>
PREFIX apple: <http://uc5.butscher.cloud/apple#>
"""


def _decide_fine_class(chat, entity: _Entity) -> str:
    """Ask LLM to pick the most specific apple:* class for this entity."""
    coarse = entity.coarse_type
    candidates = FINER_CLASSES.get(coarse, [f"apple:{coarse.title()}"])
    if len(candidates) == 1:
        return candidates[0]
    prompt = ENTITY_TYPING_TEMPLATE.format(
        name=entity.name,
        description=entity.description[:400],
        coarse_type=coarse,
        coarse_type_cap=coarse.title(),
        classes="\n".join(f"- {c}" for c in candidates),
    )
    try:
        raw = _gen_with_timeout(chat, ENTITY_TYPING_SYSTEM, prompt, timeout=20.0)
        obj = _safe_json(raw)
        if obj and isinstance(obj.get("class"), str):
            chosen = obj["class"].strip()
            if chosen in candidates:
                return chosen
    except Exception as exc:  # noqa: BLE001
        log.warning("Class typing failed for %s: %s", entity.name, exc)
    # Fallback: most generic class for this coarse type
    return candidates[0]


def _decide_sameas(chat, entity: _Entity) -> str | None:
    """Optional DBpedia sameAs link — only if confident."""
    prompt = DBPEDIA_SAMEAS_TEMPLATE.format(
        name=entity.name, type=entity.coarse_type,
        description=entity.description[:400],
    )
    try:
        raw = _gen_with_timeout(chat, DBPEDIA_SAMEAS_SYSTEM, prompt, timeout=20.0)
        obj = _safe_json(raw) or {}
        uri = obj.get("dbpedia")
        conf = float(obj.get("confidence") or 0)
        if uri and isinstance(uri, str) and conf >= 0.8 and uri.startswith("http"):
            return uri
    except Exception as exc:  # noqa: BLE001
        log.warning("sameAs decision failed for %s: %s", entity.name, exc)
    return None


def run_ue4_ingest(force: bool = False) -> UE4IngestStats:
    settings = get_settings()
    t0 = time.perf_counter()

    # Make sure the repo exists and the ontology is loaded.
    graphdb_client.apply_graphdb_setup()

    if not force:
        n = graphdb_client.count_triples()
        # Bare ontology has ~260 triples; any value much higher means there's
        # already an instance ingest in there.
        if n > 1000:
            log.info("UE4 ingest: %d triples already present — skipping (use force)", n)
            return UE4IngestStats(
                entities_typed=0, relations_inserted=0, sameas_links=0,
                llm_calls=0, triples_after=n,
                duration_ms=(time.perf_counter() - t0) * 1000,
            )

    if force:
        log.info("UE4 ingest: CLEAR ALL + re-upload ontology (force)")
        graphdb_client.clear_all()
        graphdb_client.apply_graphdb_setup()

    entities = _load_entities()
    relations = _load_relations()
    log.info("UE4 ingest: %d entities, %d relations to convert",
             len(entities), len(relations))

    chat = get_chat_llm("gemini")
    llm_calls = 0
    sameas_count = 0
    triples_batch: list[str] = []

    # ── Entities: type each, optionally sameAs ──
    for i, e in enumerate(entities):
        fine_class = _decide_fine_class(chat, e)
        llm_calls += 1
        # Try DBpedia sameAs only for the more mentioned (saves calls)
        dbp = None
        if e.mention_count >= 2 and e.coarse_type in ("PERSON", "ORGANIZATION", "PRODUCT", "LOCATION"):
            dbp = _decide_sameas(chat, e)
            llm_calls += 1
            if dbp:
                sameas_count += 1
        triples_batch.append(_build_entity_triples(e, fine_class, dbp))
        if (i + 1) % 25 == 0:
            log.info("  typed %d/%d entities (%d sameAs so far, %d LLM calls)",
                     i + 1, len(entities), sameas_count, llm_calls)

    # ── Relations: deterministic mapping ──
    for r in relations:
        triples_batch.append(_build_relation_triple(r))

    # Push everything in one big INSERT DATA — GraphDB handles this fine and
    # it's much faster than per-triple round trips.
    chunk_size = 200
    for start in range(0, len(triples_batch), chunk_size):
        block = "\n".join(triples_batch[start:start + chunk_size])
        update_query = PREFIXES + "INSERT DATA {\n" + block + "\n}"
        graphdb_client.update(update_query)

    triples_after = graphdb_client.count_triples()
    duration_ms = (time.perf_counter() - t0) * 1000
    log.info("UE4 ingest: done — %d triples now in repo (%d LLM calls)",
             triples_after, llm_calls)
    return UE4IngestStats(
        entities_typed=len(entities),
        relations_inserted=len(relations),
        sameas_links=sameas_count,
        llm_calls=llm_calls,
        triples_after=triples_after,
        duration_ms=duration_ms,
    )
