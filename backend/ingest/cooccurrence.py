"""Add co-occurrence edges between entities mentioned in the same
Wikipedia section.

UE3 extracts relations only when its LLM prompt explicitly identifies
a pair as related, which is conservative — many obvious connections
(e.g. iPhone + iOS both discussed in the same paragraph) never become
edges. Result: the graph is sparse and ego networks are thin.

This module reads all UE1 chunks, groups them by section, finds which
known UE3 entities are mentioned in each section, and for every pair
that co-occurs in N >= MIN_COOCCUR sections inserts a Neo4j edge of
type ``mentioned_with`` with weight = co-occurrence count.

Entity detection is plain case-insensitive substring search on the
canonical entity name. Fast and predictable; misses synonyms but
doesn't hallucinate connections that aren't there.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import text as sql_text

from backend.data.neo4j_client import neo4j_session
from backend.data.pg import session_scope

log = logging.getLogger(__name__)

# Minimum number of sections two entities must co-occur in before we
# create an edge. 2 is a balance: 1 produces noise (every passing
# co-mention counts), 3 cuts too aggressively on small articles.
MIN_COOCCUR = 2


def fetch_section_mentions() -> dict[str, list[str]]:
    """For every UE1 chunk's section, return the list of entity_keys
    whose canonical names appear in any chunk text of that section.

    Grouped by section_path so multi-chunk sections aggregate."""
    with session_scope() as session:
        ent_rows = session.execute(sql_text(
            "SELECT entity_key, name FROM ue3.entity_summary"
        )).mappings().all()
        # Pull all UE1 chunks joined to their section path
        chunk_rows = session.execute(sql_text(
            "SELECT s.path AS section, c.text AS body "
            "FROM ue1.chunk c LEFT JOIN clean.section s ON s.id = c.section_id"
        )).mappings().all()

    # Lowercase names for case-insensitive contains, keep at least 3 chars
    name_to_key = [(r["name"].lower(), r["entity_key"])
                   for r in ent_rows if len(r["name"]) >= 3]

    section_entities: dict[str, set[str]] = defaultdict(set)
    for row in chunk_rows:
        body = (row["body"] or "").lower()
        section = row["section"] or "(no section)"
        for name_lc, key in name_to_key:
            if name_lc in body:
                section_entities[section].add(key)
    return {s: sorted(v) for s, v in section_entities.items()}


def compute_cooccurrences() -> dict[tuple[str, str], int]:
    """Returns {(src_key, tgt_key): count} where count is the number of
    sections the two entities co-occur in. Symmetric, undirected — we
    canonicalise (src, tgt) with src <= tgt to dedupe."""
    sections = fetch_section_mentions()
    pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    for entities in sections.values():
        for i in range(len(entities)):
            for j in range(i + 1, len(entities)):
                a, b = entities[i], entities[j]
                if a == b:
                    continue
                key = (a, b) if a < b else (b, a)
                pair_counts[key] += 1
    return dict(pair_counts)


def insert_cooccurrence_edges(pair_counts: dict[tuple[str, str], int],
                              min_count: int = MIN_COOCCUR) -> dict:
    """Upsert mentioned_with edges into Neo4j. Returns stats."""
    inserted = 0
    skipped_below_threshold = 0
    with neo4j_session() as session:
        for (a, b), cnt in pair_counts.items():
            if cnt < min_count:
                skipped_below_threshold += 1
                continue
            r = session.run(
                """
                MATCH (x:Entity {id: $a}), (y:Entity {id: $b})
                MERGE (x)-[r:RELATED_TO {type: 'mentioned_with'}]->(y)
                ON CREATE SET r.weight = $w, r.source = 'cooccurrence'
                ON MATCH SET r.weight = $w
                RETURN r
                """,
                a=a, b=b, w=cnt,
            ).single()
            if r:
                inserted += 1
    return {
        "pairs_above_threshold": inserted,
        "pairs_below_threshold": skipped_below_threshold,
        "threshold": min_count,
    }


def enrich_cooccurrence() -> dict:
    """End-to-end. Returns stats for the API caller."""
    log.info("Co-occurrence: scanning UE1 chunks for entity mentions …")
    pair_counts = compute_cooccurrences()
    log.info("  found %d candidate pairs", len(pair_counts))
    stats = insert_cooccurrence_edges(pair_counts)
    stats["pairs_total"] = len(pair_counts)
    log.info("Co-occurrence done: %s", stats)
    return stats


if __name__ == "__main__":
    import json as _json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    print(_json.dumps(enrich_cooccurrence(), indent=2))
