"""UE3 ingest: build a knowledge graph from the UE1 chunks.

Pipeline:

1. For every ue1.chunk, ask Gemini to extract entities (PERSON, ORGANIZATION,
   PRODUCT, EVENT, LOCATION, CONCEPT) and relations between them.
2. Resolve entities to canonical "{type}:{normalized_name}" keys so
   "Steve Jobs" and "Jobs" land on the same node.
3. Write Entity nodes, MENTIONS edges (chunk → entity) and RELATED_TO edges
   (entity → entity) into Neo4j.
4. Compute communities via networkx greedy modularity on the Entity graph.
5. For each community ask Gemini for a 2-sentence summary, embed it, store
   in ue3.community_summary. Also store per-entity descriptions with
   embeddings in ue3.entity_summary.
6. Write back MENTIONS-counted mention_count per entity so the UI can size
   nodes by importance.

This is the most LLM-heavy part of the project: ~130 extraction calls +
~5–15 community summary calls. Cost in the cents range, runtime 2–5 min.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import networkx as nx
from sqlalchemy import text

from backend.config import get_settings
from backend.data import repo
from backend.data.neo4j_client import neo4j_session
from backend.data.pg import session_scope
from backend.llm.factory import get_chat_llm, get_embedding_llm

log = logging.getLogger(__name__)

VALID_TYPES = {"PERSON", "ORGANIZATION", "PRODUCT", "EVENT", "LOCATION", "CONCEPT"}

EXTRACTION_SYSTEM = (
    "Du bist ein gründlicher Named-Entity- und Relations-Extraktor für ein "
    "Knowledge-Graph-System über das Unternehmen Apple. Sei MAXIMAL "
    "VOLLSTÄNDIG: extrahiere JEDE Person, jedes Produkt, jede Organisation "
    "und jeden anderen relevanten Entity-Typ, der im Text erwähnt wird — "
    "auch wenn nur einmal in einem Nebensatz erwähnt. Lieber zu vollständig "
    "als zu sparsam.\n\n"
    "BESONDERS WICHTIG — historische Apple-Personen werden oft übersehen, "
    "MÜSSEN aber extrahiert werden wenn im Text:\n"
    "  • CEOs: Steve Jobs, John Sculley, Michael Spindler, Gil Amelio, "
    "    Tim Cook (auch bei kurzer Erwähnung)\n"
    "  • Founder/Frühphase: Steve Wozniak, Ronald Wayne, Mike Markkula, "
    "    Jef Raskin\n"
    "  • Designer/Engineering: Jonathan Ive (Jony Ive), Hartmut Esslinger\n"
    "  • Spätere Executives: Phil Schiller, Craig Federighi, Eddy Cue, "
    "    Angela Ahrendts, Bob Mansfield, Scott Forstall\n"
    "Wenn IRGENDEINE dieser Personen im Text vorkommt (auch in einem "
    "Halbsatz wie 'unter Sculleys Führung'), MUSS sie als PERSON extrahiert "
    "werden — mit der Rolle aus dem Kontext.\n\n"
    "Halluziniere aber NICHTS dazu, was nicht im Text steht. Personen die "
    "NICHT im Text vorkommen, NICHT extrahieren.\n\n"
    "Bei Personen IMMER den vollen Namen verwenden (nicht nur Nachname — "
    "auch wenn der Text nur 'Sculley' schreibt, dann 'John Sculley' "
    "extrahieren), und die Rolle in der Description nennen "
    "(z.B. CEO 1983-1993, Mitgründer, Designer).\n\n"
    "Typen: PERSON, ORGANIZATION, PRODUCT, EVENT, LOCATION, CONCEPT. "
    "Antworte AUSSCHLIESSLICH mit gültigem JSON, ohne Codefence."
)

EXTRACTION_TEMPLATE = """Auszug:
\"\"\"
{chunk_text}
\"\"\"

Beispiel — angenommen der Auszug wäre über die Apple-Gründung:
{{
  "entities": [
    {{"name": "Steve Jobs", "type": "PERSON", "description": "Mitgründer von Apple und CEO von 1997 bis 2011"}},
    {{"name": "Steve Wozniak", "type": "PERSON", "description": "Mitgründer von Apple, Entwickler des Apple I"}},
    {{"name": "Ronald Wayne", "type": "PERSON", "description": "Dritter Mitgründer von Apple"}},
    {{"name": "Apple", "type": "ORGANIZATION", "description": "1976 gegründetes Technologieunternehmen"}},
    {{"name": "Apple I", "type": "PRODUCT", "description": "Erster Computer von Apple, 1976"}},
    {{"name": "Cupertino", "type": "LOCATION", "description": "Hauptsitz von Apple in Kalifornien"}}
  ],
  "relations": [
    {{"source": "Steve Jobs", "target": "Apple", "type": "GRUENDET", "evidence": "Steve Jobs gründete Apple"}},
    {{"source": "Steve Wozniak", "target": "Apple I", "type": "ENTWICKELT", "evidence": "Wozniak entwickelte den Apple I"}}
  ]
}}

Beachte: voller Name bei Personen ("Steve Jobs", nicht "Jobs"). Rolle in
der Description ("CEO 1997-2011", "Mitgründer", "Designer"). JEDE im
Text erwähnte Person als eigene Entity, auch wenn sie nur kurz vorkommt.

Jetzt extrahiere alles aus dem obigen Auszug. Antworte nur mit JSON."""

COMMUNITY_SYSTEM = (
    "Du beschreibst die gemeinsame Thematik einer Gruppe verwandter Entitäten "
    "aus dem deutschen Wikipedia-Artikel über Apple. Antworte in höchstens "
    "zwei Sätzen, faktenorientiert."
)


@dataclass
class ExtractedEntity:
    name: str
    type: str
    description: str


@dataclass
class ExtractedRelation:
    source: str  # entity name from same chunk
    target: str
    type: str
    evidence: str


@dataclass
class ChunkExtraction:
    chunk_id: int
    entities: list[ExtractedEntity] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)


@dataclass
class UE3IngestStats:
    chunks_processed: int
    entities_unique: int
    relations_unique: int
    communities: int
    llm_calls: int
    duration_ms: float


# ── parsing ────────────────────────────────────────────────────────────────

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(text_: str) -> dict | None:
    if not text_:
        return None
    cleaned = text_.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    m = _JSON_OBJECT_RE.search(cleaned)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def parse_extraction(raw: str, chunk_id: int) -> ChunkExtraction:
    """Robust parse of the LLM extraction response."""
    out = ChunkExtraction(chunk_id=chunk_id)
    obj = _extract_json_object(raw)
    if not obj:
        return out
    for e in (obj.get("entities") or []):
        name = str(e.get("name") or "").strip()
        typ = str(e.get("type") or "").strip().upper()
        desc = str(e.get("description") or "").strip()
        if not name or typ not in VALID_TYPES:
            continue
        out.entities.append(ExtractedEntity(name=name[:200], type=typ, description=desc[:400]))
    name_set = {e.name for e in out.entities}
    for r in (obj.get("relations") or []):
        src = str(r.get("source") or "").strip()
        tgt = str(r.get("target") or "").strip()
        typ = str(r.get("type") or "").strip().upper().replace(" ", "_")
        ev = str(r.get("evidence") or "").strip()
        if not src or not tgt or src == tgt or not typ:
            continue
        # Only keep relations where both endpoints were also extracted as
        # entities in the same chunk (otherwise we'd dangling-link).
        if src not in name_set or tgt not in name_set:
            continue
        out.relations.append(ExtractedRelation(source=src[:200], target=tgt[:200], type=typ[:60], evidence=ev[:400]))
    return out


# ── entity resolution ─────────────────────────────────────────────────────

_PUNCT_RE = re.compile(r"[^\w\s-]", re.UNICODE)


def normalize_entity_name(name: str) -> str:
    """Lowercase, strip diacritics, drop punctuation, collapse whitespace."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _PUNCT_RE.sub("", s.lower())
    return " ".join(s.split())


def entity_key(name: str, type_: str) -> str:
    return f"{type_}:{normalize_entity_name(name)}"


def _resolve_person_aliases(
    entity_descriptions: dict[str, dict],
    mention_counter: Counter[str],
) -> dict[str, str]:
    """Merge PERSON entities where one full name is a single-word subset of
    another (e.g. "Wozniak" → "Steve Wozniak", "Jobs" → "Steve Jobs").
    Returns {alias_key: canonical_key} mapping for callers to rewrite
    relations. Modifies ``entity_descriptions`` and ``mention_counter``
    in place: the alias rows are removed, their mention counts and any
    longer description rolls into the canonical row.

    Heuristic only — could over-merge if two unrelated persons share a
    surname (rare in a single Wikipedia article on one company)."""
    persons = sorted(
        ((k, v) for k, v in entity_descriptions.items() if v["type"] == "PERSON"),
        key=lambda kv: -len(kv[1]["name"]),  # longest name first → canonical
    )
    aliases: dict[str, str] = {}
    for i, (long_key, long_v) in enumerate(persons):
        if long_key in aliases:
            continue
        long_words = set(normalize_entity_name(long_v["name"]).split())
        for short_key, short_v in persons[i + 1:]:
            if short_key in aliases or short_key == long_key:
                continue
            short_norm = normalize_entity_name(short_v["name"])
            if " " in short_norm:
                continue  # only merge single-word names into multi-word ones
            if short_norm in long_words:
                aliases[short_key] = long_key
                mention_counter[long_key] += mention_counter.get(short_key, 0)
                mention_counter[short_key] = 0
                # Prefer the longer description (more context)
                if len(short_v["description"]) > len(long_v["description"]):
                    long_v["description"] = short_v["description"]
    for alias_key in aliases:
        entity_descriptions.pop(alias_key, None)
    return aliases


# ── extraction loop ───────────────────────────────────────────────────────

def _load_chunks() -> list[tuple[int, str, str | None]]:
    """Pull (chunk_id, text, section_path) for every ue1 chunk."""
    with session_scope() as session:
        rows = session.execute(
            text(
                "SELECT c.id, c.text, s.path "
                "FROM ue1.chunk c LEFT JOIN clean.section s ON s.id = c.section_id "
                "ORDER BY c.id"
            )
        ).all()
    return [(int(r[0]), r[1], r[2]) for r in rows]


EXTRACTION_TIMEOUT_SEC = 90      # hard cutoff per Gemini call
EXTRACTION_MAX_ATTEMPTS = 3      # incl. first try

_extraction_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="ue3-extract"
)


def _generate_with_timeout(chat, system: str, user: str, timeout: float) -> str:
    """Run a sync Gemini call with a hard wall-clock limit. If the underlying
    HTTP request hangs (no SDK timeout), we abandon the thread and raise. The
    abandoned thread keeps running until the process exits, but the pipeline
    stays unblocked."""
    fut = _extraction_executor.submit(chat.generate, system, user)
    try:
        raw, _usage = fut.result(timeout=timeout)
        return raw or ""
    except concurrent.futures.TimeoutError as exc:
        fut.cancel()
        raise TimeoutError(f"Gemini call exceeded {timeout:.0f}s") from exc


def _extract_one(chat, chunk_text: str, chunk_id: int) -> tuple[ChunkExtraction, int]:
    """Run the extraction LLM call for one chunk with timeout + retries.
    Returns (extraction, calls). Calls counts every attempt incl. retries."""
    prompt = EXTRACTION_TEMPLATE.format(chunk_text=chunk_text)
    attempts = 0
    backoff = 2.0
    while attempts < EXTRACTION_MAX_ATTEMPTS:
        attempts += 1
        try:
            raw = _generate_with_timeout(chat, EXTRACTION_SYSTEM, prompt,
                                          EXTRACTION_TIMEOUT_SEC)
            return parse_extraction(raw, chunk_id), attempts
        except TimeoutError as exc:
            log.warning("Chunk %d extraction timed out (attempt %d/%d): %s",
                        chunk_id, attempts, EXTRACTION_MAX_ATTEMPTS, exc)
        except Exception as exc:  # noqa: BLE001
            # 429s, transient API errors, JSON-decode etc. fall here.
            log.warning("Chunk %d extraction failed (attempt %d/%d): %s",
                        chunk_id, attempts, EXTRACTION_MAX_ATTEMPTS, exc)
        if attempts < EXTRACTION_MAX_ATTEMPTS:
            time.sleep(backoff)
            backoff = min(backoff * 2, 20.0)
    log.error("Chunk %d extraction giving up after %d attempts", chunk_id, attempts)
    return ChunkExtraction(chunk_id=chunk_id), attempts


# ── Neo4j writeback ───────────────────────────────────────────────────────

def _wipe_neo4j() -> None:
    with neo4j_session() as session:
        session.run("MATCH (n) WHERE NOT n:Migration DETACH DELETE n").consume()


def _write_to_neo4j(
    chunk_rows: list[tuple[int, str, str | None]],
    extractions: list[ChunkExtraction],
    entity_descriptions: dict[str, dict],
    aliases: dict[str, str] | None = None,
) -> None:
    """Write Chunk, Entity, MENTIONS and RELATED_TO into Neo4j in batches.
    ``aliases`` maps merged extraction keys onto their canonical key so
    MENTIONS/RELATED_TO edges land on the surviving Entity node."""
    aliases = aliases or {}

    def canon(k: str) -> str:
        return aliases.get(k, k)

    with neo4j_session() as session:
        # Chunks
        session.run(
            "UNWIND $rows AS r "
            "MERGE (c:Chunk {id: r.id}) "
            "SET c.text = r.text, c.section_path = r.section_path",
            rows=[{"id": cid, "text": txt or "", "section_path": sp or ""}
                  for cid, txt, sp in chunk_rows],
        ).consume()

        # Entities
        entity_rows = [
            {
                "key": k,
                "name": v["name"],
                "type": v["type"],
                "description": v["description"],
            }
            for k, v in entity_descriptions.items()
        ]
        session.run(
            "UNWIND $rows AS r "
            "MERGE (e:Entity {id: r.key}) "
            "SET e.name = r.name, e.type = r.type, e.description = r.description",
            rows=entity_rows,
        ).consume()

        # MENTIONS edges — push through canonical-key map so merged aliases
        # land on the surviving Entity node.
        mentions_rows = []
        for ext in extractions:
            for e in ext.entities:
                mentions_rows.append({"cid": ext.chunk_id, "ekey": canon(entity_key(e.name, e.type))})
        if mentions_rows:
            session.run(
                "UNWIND $rows AS r "
                "MATCH (c:Chunk {id: r.cid}) "
                "MATCH (e:Entity {id: r.ekey}) "
                "MERGE (c)-[:MENTIONS]->(e)",
                rows=mentions_rows,
            ).consume()

        # RELATED_TO edges — also via canonical-key map.
        rel_rows = []
        for ext in extractions:
            name_to_type = {e.name: e.type for e in ext.entities}
            for r in ext.relations:
                src_type = name_to_type.get(r.source)
                tgt_type = name_to_type.get(r.target)
                if not src_type or not tgt_type:
                    continue
                src_key = canon(entity_key(r.source, src_type))
                tgt_key = canon(entity_key(r.target, tgt_type))
                if src_key == tgt_key:
                    continue
                rel_rows.append({
                    "src": src_key,
                    "tgt": tgt_key,
                    "type": r.type,
                    "ev": r.evidence,
                })
        if rel_rows:
            session.run(
                "UNWIND $rows AS r "
                "MATCH (a:Entity {id: r.src}) "
                "MATCH (b:Entity {id: r.tgt}) "
                "MERGE (a)-[rel:RELATED_TO {type: r.type}]->(b) "
                "ON CREATE SET rel.weight = 1, rel.evidence = r.ev "
                "ON MATCH SET rel.weight = rel.weight + 1",
                rows=rel_rows,
            ).consume()


# ── community detection (NetworkX greedy modularity) ──────────────────────

def _build_networkx_graph(entity_descriptions: dict[str, dict],
                          relations: list[tuple[str, str, int]]) -> nx.Graph:
    g = nx.Graph()
    for k in entity_descriptions:
        g.add_node(k)
    for src, tgt, w in relations:
        if src == tgt:
            continue
        if g.has_edge(src, tgt):
            g[src][tgt]["weight"] = g[src][tgt].get("weight", 1) + w
        else:
            g.add_edge(src, tgt, weight=w)
    return g


def _detect_communities(g: nx.Graph) -> list[set[str]]:
    if g.number_of_nodes() == 0:
        return []
    # greedy_modularity_communities handles disconnected graphs by treating
    # each component independently; small components become their own
    # community. weight='weight' uses the RELATED_TO co-occurrence count.
    try:
        comms = list(nx.community.greedy_modularity_communities(g, weight="weight"))
    except Exception as exc:  # noqa: BLE001
        log.warning("Community detection failed (%s) — falling back to connected components", exc)
        comms = [set(c) for c in nx.connected_components(g)]
    return [set(c) for c in comms]


def _community_summary(chat, member_descriptions: list[tuple[str, str, str]]) -> str:
    """Ask Gemini to summarise a community. ``member_descriptions`` items are
    (name, type, description)."""
    if not member_descriptions:
        return ""
    lines = [f"- {n} ({t}): {d}" for n, t, d in member_descriptions[:20]]
    prompt = (
        "Entitäten in dieser Gruppe:\n" + "\n".join(lines) + "\n\n"
        "Welche gemeinsame Thematik verbindet diese Entitäten im Apple-Kontext? "
        "Maximal zwei Sätze."
    )
    try:
        answer, _ = chat.generate(COMMUNITY_SYSTEM, prompt)
    except Exception as exc:  # noqa: BLE001
        log.warning("Community summary LLM failed: %s", exc)
        return ", ".join(n for n, _, _ in member_descriptions[:8])
    return (answer or "").strip()[:1000]


# ── main ingest ───────────────────────────────────────────────────────────

def run_ue3_ingest(force: bool = False) -> UE3IngestStats:
    settings = get_settings()
    t0 = time.perf_counter()

    chunk_rows = _load_chunks()
    if not chunk_rows:
        raise RuntimeError("UE3 ingest: no UE1 chunks found — run UE1 ingest first.")

    if not force:
        with session_scope() as session:
            if repo.ue3_entity_count(session) > 0:
                log.info("UE3 ingest: already populated — skipping (use force to rebuild)")
                return UE3IngestStats(
                    chunks_processed=0,
                    entities_unique=repo.ue3_entity_count(session),
                    relations_unique=0,
                    communities=repo.ue3_community_count(session),
                    llm_calls=0,
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )

    chat = get_chat_llm("gemini")
    embedder = get_embedding_llm()

    log.info("UE3 ingest: extracting entities from %d chunks", len(chunk_rows))
    extractions: list[ChunkExtraction] = []
    llm_calls = 0
    t_extract = time.perf_counter()
    for cid, txt, _path in chunk_rows:
        if not (txt or "").strip():
            extractions.append(ChunkExtraction(chunk_id=cid))
            continue
        ext, calls = _extract_one(chat, txt, cid)
        llm_calls += calls
        extractions.append(ext)
        if len(extractions) % 10 == 0:
            elapsed = time.perf_counter() - t_extract
            rate = len(extractions) / max(elapsed, 0.1)
            remaining = (len(chunk_rows) - len(extractions)) / max(rate, 0.01)
            log.info("  extracted %d/%d chunks (%d LLM calls, %.1f c/s, ~%.0fs left)",
                     len(extractions), len(chunk_rows), llm_calls, rate, remaining)

    # ── Resolve entities to canonical keys; aggregate descriptions ──
    entity_descriptions: dict[str, dict] = {}
    mention_counter: Counter[str] = Counter()
    for ext in extractions:
        for e in ext.entities:
            k = entity_key(e.name, e.type)
            mention_counter[k] += 1
            existing = entity_descriptions.get(k)
            if existing is None:
                entity_descriptions[k] = {
                    "name": e.name,
                    "type": e.type,
                    "description": e.description,
                }
            elif len(e.description) > len(existing["description"]):
                existing["description"] = e.description

    # ── Merge person aliases (e.g. "Wozniak" → "Steve Wozniak") ──
    person_aliases = _resolve_person_aliases(entity_descriptions, mention_counter)
    if person_aliases:
        log.info("UE3 ingest: merged %d person aliases", len(person_aliases))

    # Co-occurrence relation list (sum weights across chunks). Rewrites
    # source/target through person_aliases so a relation "Wozniak →
    # entwickelt → Apple I" lands on the canonical "Steve Wozniak" key.
    def _canonical(k: str) -> str:
        return person_aliases.get(k, k)

    rel_weights: defaultdict[tuple[str, str], int] = defaultdict(int)
    for ext in extractions:
        name_to_type = {e.name: e.type for e in ext.entities}
        for r in ext.relations:
            st = name_to_type.get(r.source)
            tt = name_to_type.get(r.target)
            if not st or not tt:
                continue
            a, b = _canonical(entity_key(r.source, st)), _canonical(entity_key(r.target, tt))
            if a == b:
                continue
            pair = (a, b) if a < b else (b, a)
            rel_weights[pair] += 1
    relations_unique = list((a, b, w) for (a, b), w in rel_weights.items())
    log.info("UE3 ingest: %d unique entities, %d unique relations",
             len(entity_descriptions), len(relations_unique))

    # ── Write Neo4j (wipe first when forcing) ──
    if force:
        _wipe_neo4j()
    _write_to_neo4j(chunk_rows, extractions, entity_descriptions, aliases=person_aliases)

    # ── Community detection ──
    g = _build_networkx_graph(entity_descriptions, relations_unique)
    communities = _detect_communities(g)
    log.info("UE3 ingest: %d communities detected", len(communities))

    # ── Per-entity embeddings ──
    BATCH = 100
    entity_keys = list(entity_descriptions.keys())
    entity_embed_texts = [
        f"{entity_descriptions[k]['type']}: {entity_descriptions[k]['name']}. "
        f"{entity_descriptions[k]['description']}"
        for k in entity_keys
    ]
    entity_embeddings: list[list[float]] = []
    for i in range(0, len(entity_embed_texts), BATCH):
        entity_embeddings.extend(embedder.embed(entity_embed_texts[i:i + BATCH]))

    # ── Per-community LLM summaries + embeddings ──
    community_summaries: list[tuple[str, int, list[str], str]] = []
    for idx, members in enumerate(communities):
        sorted_members = sorted(members, key=lambda k: -mention_counter[k])
        member_descs = [
            (entity_descriptions[k]["name"],
             entity_descriptions[k]["type"],
             entity_descriptions[k]["description"])
            for k in sorted_members
        ]
        summary = _community_summary(chat, member_descs)
        llm_calls += 1
        community_summaries.append((f"c{idx:03d}", 0, sorted_members, summary))

    community_embeddings: list[list[float]] = []
    sum_texts = [s[3] or "(leer)" for s in community_summaries]
    for i in range(0, len(sum_texts), BATCH):
        community_embeddings.extend(embedder.embed(sum_texts[i:i + BATCH]))

    # ── Write back ue3.entity_summary + ue3.community_summary ──
    with session_scope() as session:
        repo.delete_ue3_summaries(session)
    with session_scope() as session:
        for k, emb in zip(entity_keys, entity_embeddings):
            d = entity_descriptions[k]
            repo.upsert_entity_summary(
                session,
                entity_key=k, name=d["name"], type_=d["type"],
                description=d["description"],
                mention_count=mention_counter[k],
                embedding=emb,
            )
        for (cid, lvl, members, summary), emb in zip(community_summaries, community_embeddings):
            repo.upsert_community_summary(
                session,
                community_id=cid, level=lvl,
                size=len(members), summary=summary,
                entity_keys=members, embedding=emb,
            )

    # ── Stamp community membership back into Neo4j ──
    with neo4j_session() as session:
        for cid, lvl, members, summary in community_summaries:
            session.run(
                "MERGE (com:Community {id: $id}) "
                "SET com.level = $lvl, com.size = $sz, com.summary = $sum",
                id=cid, lvl=lvl, sz=len(members), sum=summary,
            ).consume()
            if members:
                session.run(
                    "MATCH (com:Community {id: $id}) "
                    "WITH com UNWIND $keys AS k "
                    "MATCH (e:Entity {id: k}) "
                    "MERGE (e)-[:IN_COMMUNITY]->(com)",
                    id=cid, keys=list(members),
                ).consume()

    duration_ms = (time.perf_counter() - t0) * 1000
    log.info("UE3 ingest: done in %.1f ms with %d LLM calls", duration_ms, llm_calls)
    return UE3IngestStats(
        chunks_processed=len(chunk_rows),
        entities_unique=len(entity_descriptions),
        relations_unique=len(relations_unique),
        communities=len(communities),
        llm_calls=llm_calls,
        duration_ms=duration_ms,
    )
