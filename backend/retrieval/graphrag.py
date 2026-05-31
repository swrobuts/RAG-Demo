"""UE3 — GraphRAG retrieval in three flavours.

* **local**  — entity-centric. Find top-k entities most similar to the query
  (via ue3.entity_summary embedding), traverse 1 hop in Neo4j to pull in
  related entities, gather the chunks that MENTION any of these entities.
* **global** — community-centric. Find top-k communities by summary
  embedding similarity; each community contributes its summary as a pseudo-
  chunk so the answering LLM can map-reduce.
* **hybrid** (default) — runs both and unions/dedupes the chunks.

All three return the standard ``RetrievalResult`` so /api/query and
/api/compare see UE3 the same way they see UE1/UE2.
"""
from __future__ import annotations

import logging
import re
import time

from backend.config import get_settings
from backend.data import repo
from backend.data.neo4j_client import neo4j_session
from backend.data.pg import session_scope
from backend.llm.factory import get_embedding_llm
from backend.retrieval.base import Chunk, RetrievalResult, SourceRef

log = logging.getLogger(__name__)

VALID_MODES = ("local", "global", "hybrid")

# Tokens hinting that the user is asking about entities of a given type.
# Keyed by type name; values are German trigger words (lowercased).
_TYPE_HINTS: dict[str, tuple[str, ...]] = {
    "PERSON": ("wer", "personen", "person", "mitarbeiter", "gründer", "ceo",
               "vorstand", "manager", "chef", "designer", "entwickler"),
    "PRODUCT": ("produkte", "produkt", "geräte", "gerät", "modell", "modelle",
                "software", "hardware", "app", "apps"),
    "ORGANIZATION": ("firma", "unternehmen", "konzern", "organisation",
                     "tochterfirma", "tochterfirmen", "investor", "investoren"),
    "EVENT": ("ereignis", "ereignisse", "veranstaltung", "event"),
    "LOCATION": ("ort", "orte", "standort", "standorte", "stadt", "städte",
                 "land", "länder", "region", "regionen"),
    "CONCEPT": ("konzept", "konzepte", "technologie", "technologien"),
}

# Common German stopwords (small list — only what matters for keyword extraction).
_STOPWORDS = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer", "einem", "einen", "eines",
    "von", "vom", "zu", "zum", "zur", "in", "im", "an", "am", "auf", "für", "mit", "bei",
    "wer", "was", "wann", "wie", "welche", "welches", "welcher", "alle", "alles", "jeder",
    "ist", "sind", "war", "waren", "hat", "haben", "wird", "werden", "wurde", "wurden",
    "und", "oder", "aber", "nicht", "ja", "nein", "diese", "dieses", "dieser", "diesen",
    "nach", "vor", "über", "unter", "zwischen", "durch", "während", "seit", "bis",
    "auch", "noch", "schon", "nur", "sehr", "mehr", "viele", "vieler", "vielen",
    "bitte", "danke", "ich", "du", "er", "sie", "es", "wir", "ihr",
}


def extract_type_hints(query: str) -> list[str]:
    """Return likely entity types the query is asking about, based on
    German trigger words. Empty list means "no specific type — accept all"."""
    q = query.lower()
    hits: list[str] = []
    for type_, words in _TYPE_HINTS.items():
        if any(re.search(rf"\b{re.escape(w)}\b", q) for w in words):
            hits.append(type_)
    return hits


def extract_keywords(query: str) -> list[str]:
    """Pull keyword candidates from the query for ILIKE entity matching.
    Filters out short tokens, digits, and a small German stopword list.
    Lowercase return."""
    tokens = re.findall(r"[A-Za-zÄÖÜäöüß]{3,}", query)
    out: list[str] = []
    for t in tokens:
        tl = t.lower()
        if tl in _STOPWORDS:
            continue
        if tl not in out:
            out.append(tl)
    return out[:8]   # cap so SQL doesn't explode


class GraphRAG:
    name = "ue3"

    def __init__(self, llm_provider: str = "gemini", mode: str | None = None) -> None:
        self._llm_provider = llm_provider
        self._mode = (mode or get_settings().ue3_default_mode).lower()
        if self._mode not in VALID_MODES:
            self._mode = "hybrid"

    def retrieve(self, query: str, k: int | None = None) -> RetrievalResult:
        settings = get_settings()
        k = k or settings.ue3_top_k_chunks

        # Parse the query first — drives both entity match (type hint) and
        # the text-based ILIKE fallback for category questions like
        # "Wer waren alle CEOs von Apple?" where instance-level embeddings
        # (Steve Jobs, Tim Cook, …) don't semantically match the query.
        type_hints = extract_type_hints(query)
        keywords = extract_keywords(query)

        embedder = get_embedding_llm()
        t0 = time.perf_counter()
        query_emb = embedder.embed([query])[0]
        embed_ms = (time.perf_counter() - t0) * 1000

        local_chunks: list[dict] = []
        global_chunks: list[dict] = []
        matched_entities: list[dict] = []
        matched_communities: list[dict] = []

        t_local = 0.0
        t_global = 0.0

        if self._mode in ("local", "hybrid"):
            t0 = time.perf_counter()
            local_chunks, matched_entities = _local_retrieve(
                query_emb=query_emb,
                keywords=keywords,
                type_hints=type_hints,
                k=k,
            )
            t_local = (time.perf_counter() - t0) * 1000

        if self._mode in ("global", "hybrid"):
            t0 = time.perf_counter()
            global_chunks, matched_communities = _global_retrieve(query_emb, k=k)
            t_global = (time.perf_counter() - t0) * 1000

        # Merge local + global chunks (local first), dedupe by chunk identity.
        merged: list[dict] = []
        seen_keys: set[tuple[str, int | None]] = set()
        for c in [*local_chunks, *global_chunks]:
            key = (c["kind"], c.get("chunk_id"))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged.append(c)
        merged = merged[:k]

        # ── Text fallback / augmentation ─────────────────────────────────
        # Trigger when:
        #   a) merged is empty (no graph hit at all), OR
        #   b) merged is "thin" — fewer than ½·k chunks, OR
        #   c) every matched_entity is a CANON: stub (synced from GraphDB
        #      with only a 1-line description) and we'd otherwise feed
        #      the LLM just those stubs.
        # The fallback AUGMENTS the graph chunks with UE1 text chunks
        # (deduped) — we don't replace, because graph chunks may still
        # carry useful structural context.
        text_fallback_used = False
        # ANY CANON match → augment (CANON entities have no associated
        # text chunks, only a 1-line synthetic description, so the LLM
        # needs real article text to answer).
        any_canonical = any(
            (e.get("key") or e.get("entity_key") or "").startswith("CANON:")
            for e in matched_entities
        )
        thin_match = len(merged) < max(1, k // 2)
        # Also check: does any of the existing chunks actually mention
        # the question's key terms? Cheap substring test against the
        # query (split into 3+ char words). If none do → augment.
        query_terms = [w.lower() for w in re.findall(r"\w{3,}", query)]
        chunks_match_query = any(
            any(t in (c.get("text") or "").lower() for t in query_terms)
            for c in merged
        )
        if not merged or thin_match or any_canonical or not chunks_match_query:
            try:
                from backend.retrieval.simple import SimpleRAG
                fb = SimpleRAG(llm_provider=self._llm_provider).retrieve(query, k=k)
                if fb.chunks:
                    # Dedup by chunk_id (graph chunks first → text fills gaps)
                    have_ids = {c.chunk_id for c in (
                        [Chunk(text=c["text"], section_path=c.get("section_path"),
                               chunk_id=c.get("chunk_id")) for c in merged]
                    ) if c.chunk_id is not None}
                    augment = [c for c in fb.chunks if c.chunk_id not in have_ids]
                    # Build final chunks: graph first, then text augment
                    chunks = [Chunk(text=c["text"], section_path=c.get("section_path"),
                                    chunk_id=c.get("chunk_id")) for c in merged] + augment[: k]
                    sources = [SourceRef(chunk_id=c.get("chunk_id"),
                                          section_path=c.get("section_path") or c.get("kind"),
                                          text=c["text"], distance=c.get("distance"))
                               for c in merged] + [s for s in fb.sources
                                                     if s.chunk_id not in have_ids][: k]
                    text_fallback_used = True
            except Exception as exc:  # noqa: BLE001
                log.warning("UE3 text fallback failed: %s", exc)

        chunks = [
            Chunk(
                text=c["text"],
                section_path=c.get("section_path"),
                chunk_id=c.get("chunk_id"),
            )
            for c in merged
        ] if not text_fallback_used else chunks  # keep fallback chunks
        sources = [
            SourceRef(
                chunk_id=c.get("chunk_id"),
                section_path=c.get("section_path") or c.get("kind"),
                text=c["text"],
                distance=c.get("distance"),
            )
            for c in merged
        ] if not text_fallback_used else sources

        trace = {
            "strategy": self.name,
            "mode": self._mode,
            "llm_provider": self._llm_provider,
            "k": k,
            "embed_ms": round(embed_ms, 1),
            "local_ms": round(t_local, 1),
            "global_ms": round(t_global, 1),
            "query_keywords": keywords,
            "query_type_hints": type_hints,
            "matched_entities": [
                {"key": e["entity_key"], "name": e["name"], "type": e["type"],
                 "distance": round(e["distance"], 4), "source": e.get("source", "?")}
                for e in matched_entities
            ],
            "matched_communities": [
                {"id": c["community_id"], "level": c["level"],
                 "distance": round(c["distance"], 4)}
                for c in matched_communities
            ],
            "local_chunk_count": len(local_chunks),
            "global_chunk_count": len(global_chunks),
            "final_chunk_count": len(chunks),
            "text_fallback_used": text_fallback_used,
        }
        return RetrievalResult(chunks=chunks, sources=sources, trace=trace)


# ── local ──────────────────────────────────────────────────────────────────

def _local_retrieve(
    *,
    query_emb: list[float],
    keywords: list[str],
    type_hints: list[str],
    k: int,
) -> tuple[list[dict], list[dict]]:
    """Entity match (embedding + text) → 1-hop expand → MENTIONS chunks.

    Two retrievers, results unioned and deduped:
    1. Embedding cosine over ue3.entity_summary — semantic match.
    2. ILIKE on entity name + description, filtered by type hint if present —
       catches category questions like "wer sind die CEOs" where embeddings
       don't bridge query→instance.
    """
    settings = get_settings()
    n_each = settings.ue3_top_k_entities
    with session_scope() as session:
        emb_matches = repo.topk_entities_by_embedding(session, query_emb, n_each)
        text_matches = repo.topk_entities_by_text(
            session, keywords, n_each, type_filter=type_hints,
        )

    # Combine: text matches first (they're usually more on-topic for
    # category questions), then embedding matches. Dedupe by entity_key.
    seen: set[str] = set()
    entity_matches: list[repo.EntityMatch] = []
    source_by_key: dict[str, str] = {}
    for e in text_matches:
        if e.entity_key in seen:
            continue
        seen.add(e.entity_key)
        source_by_key[e.entity_key] = "text"
        entity_matches.append(e)
    for e in emb_matches:
        if e.entity_key in seen:
            continue
        seen.add(e.entity_key)
        source_by_key[e.entity_key] = "embedding"
        entity_matches.append(e)

    # If a type hint was given, prefer matching entities of that type at the
    # top of the list (without dropping the others — they may still help).
    if type_hints:
        entity_matches.sort(key=lambda e: (e.type not in type_hints, 0))

    if not entity_matches:
        return [], []

    matched_dicts = [
        {"entity_key": e.entity_key, "name": e.name, "type": e.type,
         "description": e.description, "distance": e.distance,
         "source": source_by_key.get(e.entity_key, "?")}
        for e in entity_matches
    ]

    seed_keys = [e.entity_key for e in entity_matches]
    # Expand 1 hop via Neo4j.
    with neo4j_session() as session:
        result = session.run(
            "UNWIND $keys AS k "
            "MATCH (seed:Entity {id: k}) "
            "OPTIONAL MATCH (seed)-[:RELATED_TO]-(neighbor:Entity) "
            "WITH seed, collect(DISTINCT neighbor.id) AS neighbors "
            "RETURN seed.id AS sid, neighbors",
            keys=seed_keys,
        )
        all_entity_keys: set[str] = set(seed_keys)
        for row in result:
            for nb in row["neighbors"]:
                if nb:
                    all_entity_keys.add(nb)

        # Fetch chunks that mention any of these entities, with the mention
        # count so we can rank.
        chunk_rows = session.run(
            "UNWIND $keys AS k "
            "MATCH (e:Entity {id: k})<-[:MENTIONS]-(c:Chunk) "
            "WITH c, count(DISTINCT e) AS hits "
            "ORDER BY hits DESC, c.id ASC "
            "LIMIT $limit "
            "RETURN c.id AS id, c.text AS text, c.section_path AS section, hits",
            keys=list(all_entity_keys),
            limit=k * settings.ue3_max_chunks_per_entity,
        ).data()

    # Top-k by hit count, capped at requested k
    chunk_rows = chunk_rows[:k]
    chunks = [
        {
            "kind": "entity_chunk",
            "chunk_id": int(row["id"]),
            "text": row["text"] or "",
            "section_path": row["section"] or "",
            # Approximate "distance" as 1 / hits so smaller is better
            "distance": 1.0 / max(int(row["hits"]), 1),
        }
        for row in chunk_rows
    ]
    return chunks, matched_dicts


# ── global ─────────────────────────────────────────────────────────────────

def _global_retrieve(
    query_emb: list[float],
    *,
    k: int,
) -> tuple[list[dict], list[dict]]:
    """Community summaries as pseudo-chunks for map-reduce style answering."""
    settings = get_settings()
    with session_scope() as session:
        comms = repo.topk_communities_by_embedding(
            session, query_emb, settings.ue3_top_k_communities,
        )
    matched = [
        {"community_id": c.community_id, "level": c.level,
         "summary": c.summary, "distance": c.distance}
        for c in comms
    ]
    chunks = [
        {
            "kind": "community_summary",
            "chunk_id": None,
            "text": f"[Community {c.community_id}]\n{c.summary}",
            "section_path": f"Community {c.community_id}",
            "distance": c.distance,
        }
        for c in comms
    ]
    return chunks, matched
