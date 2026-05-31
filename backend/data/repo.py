"""Thin data-access helpers — every retrieval strategy goes through these,
so the SQL stays in one place and the business logic stays declarative."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence

from pgvector.sqlalchemy import Vector  # noqa: F401  (registers the type)
from sqlalchemy import text
from sqlalchemy.orm import Session


# ── raw ────────────────────────────────────────────────────────────────────

def insert_or_get_snapshot(
    session: Session,
    *,
    url: str,
    html: str,
    content_hash: str,
    revision_id: str | None,
    etag: str | None,
) -> tuple[int, bool]:
    """Insert a snapshot or return the existing one if the hash matches.

    Returns (snapshot_id, created).
    """
    existing = session.execute(
        text(
            "SELECT id FROM raw.wikipedia_snapshot "
            "WHERE url = :url AND content_hash = :h"
        ),
        {"url": url, "h": content_hash},
    ).scalar()
    if existing is not None:
        return int(existing), False

    new_id = session.execute(
        text(
            "INSERT INTO raw.wikipedia_snapshot "
            "(url, html, content_hash, revision_id, etag) "
            "VALUES (:url, :html, :h, :rev, :etag) "
            "RETURNING id"
        ),
        {"url": url, "html": html, "h": content_hash, "rev": revision_id, "etag": etag},
    ).scalar()
    return int(new_id), True


def latest_snapshot(session: Session, url: str) -> dict | None:
    row = session.execute(
        text(
            "SELECT id, url, fetched_at, revision_id, content_hash "
            "FROM raw.wikipedia_snapshot WHERE url = :url "
            "ORDER BY fetched_at DESC LIMIT 1"
        ),
        {"url": url},
    ).mappings().first()
    return dict(row) if row else None


# ── clean ──────────────────────────────────────────────────────────────────

def upsert_clean_document(
    session: Session,
    *,
    snapshot_id: int,
    title: str,
    markdown: str,
) -> int:
    existing = session.execute(
        text("SELECT id FROM clean.document WHERE snapshot_id = :s"),
        {"s": snapshot_id},
    ).scalar()
    if existing is not None:
        # Keep the document idempotent for the same snapshot.
        session.execute(
            text("DELETE FROM clean.section WHERE document_id = :d"),
            {"d": existing},
        )
        return int(existing)
    new_id = session.execute(
        text(
            "INSERT INTO clean.document (snapshot_id, title, markdown) "
            "VALUES (:s, :t, :m) RETURNING id"
        ),
        {"s": snapshot_id, "t": title, "m": markdown},
    ).scalar()
    return int(new_id)


def insert_section(
    session: Session,
    *,
    document_id: int,
    parent_id: int | None,
    level: int,
    heading: str,
    path: str,
    order_idx: int,
    text_: str,
) -> int:
    new_id = session.execute(
        text(
            "INSERT INTO clean.section "
            "(document_id, parent_id, level, heading, path, order_idx, text) "
            "VALUES (:d, :p, :l, :h, :path, :o, :txt) RETURNING id"
        ),
        {"d": document_id, "p": parent_id, "l": level, "h": heading,
         "path": path, "o": order_idx, "txt": text_},
    ).scalar()
    return int(new_id)


# ── ue1 ────────────────────────────────────────────────────────────────────

def delete_ue1_chunks(session: Session, document_id: int) -> None:
    session.execute(
        text("DELETE FROM ue1.chunk WHERE document_id = :d"),
        {"d": document_id},
    )


def insert_ue1_chunk(
    session: Session,
    *,
    document_id: int,
    section_id: int | None,
    order_idx: int,
    chunk_text: str,
    token_count: int,
    embedding: list[float],
) -> None:
    session.execute(
        text(
            "INSERT INTO ue1.chunk "
            "(document_id, section_id, order_idx, text, token_count, embedding) "
            "VALUES (:d, :s, :o, :t, :tc, :e)"
        ),
        {"d": document_id, "s": section_id, "o": order_idx,
         "t": chunk_text, "tc": token_count, "e": str(embedding)},
    )


@dataclass
class RetrievedChunk:
    chunk_id: int
    section_id: int | None
    section_path: str | None
    text: str
    distance: float        # smaller is better for dense; for BM25 we negate ts_rank so order stays consistent
    order_idx: int
    embedding: list[float] | None = None   # filled when caller asks for it (MMR)


def topk_ue1(
    session: Session,
    query_embedding: Sequence[float],
    k: int,
    *,
    with_embedding: bool = False,
) -> list[RetrievedChunk]:
    extra = ", c.embedding" if with_embedding else ""
    rows = session.execute(
        text(
            f"SELECT c.id, c.section_id, s.path AS section_path, c.text, "
            f"       c.order_idx, "
            f"       c.embedding <=> CAST(:q AS vector) AS distance "
            f"       {extra} "
            f"FROM ue1.chunk c "
            f"LEFT JOIN clean.section s ON s.id = c.section_id "
            f"ORDER BY distance ASC LIMIT :k"
        ),
        {"q": str(list(query_embedding)), "k": k},
    ).mappings().all()
    return [_row_to_retrieved_chunk(r, with_embedding) for r in rows]


def topk_ue1_bm25(
    session: Session,
    query: str,
    k: int,
    *,
    with_embedding: bool = False,
) -> list[RetrievedChunk]:
    """Postgres FTS top-k with German stemming. The score is ts_rank_cd; we
    store its negation as ``distance`` so the consumer sees a smaller-is-better
    number across both retrievers."""
    extra = ", c.embedding" if with_embedding else ""
    rows = session.execute(
        text(
            f"SELECT c.id, c.section_id, s.path AS section_path, c.text, "
            f"       c.order_idx, "
            f"       -ts_rank_cd(c.tsv, plainto_tsquery('german', :q)) AS distance "
            f"       {extra} "
            f"FROM ue1.chunk c "
            f"LEFT JOIN clean.section s ON s.id = c.section_id "
            f"WHERE c.tsv @@ plainto_tsquery('german', :q) "
            f"ORDER BY distance ASC LIMIT :k"
        ),
        {"q": query, "k": k},
    ).mappings().all()
    return [_row_to_retrieved_chunk(r, with_embedding) for r in rows]


def ue1_chunk_count(session: Session, document_id: int | None = None) -> int:
    if document_id is None:
        return int(session.execute(text("SELECT COUNT(*) FROM ue1.chunk")).scalar() or 0)
    return int(
        session.execute(
            text("SELECT COUNT(*) FROM ue1.chunk WHERE document_id = :d"),
            {"d": document_id},
        ).scalar()
        or 0
    )


def _subtree_where_clause(section_paths: Sequence[str], params: dict) -> str:
    parts = []
    for i, p in enumerate(section_paths):
        parts.append(f"s.path = :p{i} OR s.path LIKE :pp{i}")
        params[f"p{i}"] = p
        params[f"pp{i}"] = p + " > %"
    return "(" + " OR ".join(parts) + ")"


def topk_ue1_in_subtree(
    session: Session,
    query_embedding: Sequence[float],
    k: int,
    section_paths: Sequence[str],
    *,
    with_embedding: bool = False,
) -> list[RetrievedChunk]:
    """Dense top-k restricted to chunks below any of ``section_paths``."""
    if not section_paths:
        return []
    extra = ", c.embedding" if with_embedding else ""
    params: dict[str, object] = {"q": str(list(query_embedding)), "k": k}
    where = _subtree_where_clause(section_paths, params)
    rows = session.execute(
        text(
            f"SELECT c.id, c.section_id, s.path AS section_path, c.text, "
            f"       c.order_idx, "
            f"       c.embedding <=> CAST(:q AS vector) AS distance "
            f"       {extra} "
            f"FROM ue1.chunk c "
            f"JOIN clean.section s ON s.id = c.section_id "
            f"WHERE {where} "
            f"ORDER BY distance ASC LIMIT :k"
        ),
        params,
    ).mappings().all()
    return [_row_to_retrieved_chunk(r, with_embedding) for r in rows]


def topk_ue1_bm25_in_subtree(
    session: Session,
    query: str,
    k: int,
    section_paths: Sequence[str],
    *,
    with_embedding: bool = False,
) -> list[RetrievedChunk]:
    """BM25 top-k restricted to chunks below any of ``section_paths``."""
    if not section_paths:
        return []
    extra = ", c.embedding" if with_embedding else ""
    params: dict[str, object] = {"q": query, "k": k}
    where = _subtree_where_clause(section_paths, params)
    rows = session.execute(
        text(
            f"SELECT c.id, c.section_id, s.path AS section_path, c.text, "
            f"       c.order_idx, "
            f"       -ts_rank_cd(c.tsv, plainto_tsquery('german', :q)) AS distance "
            f"       {extra} "
            f"FROM ue1.chunk c "
            f"JOIN clean.section s ON s.id = c.section_id "
            f"WHERE c.tsv @@ plainto_tsquery('german', :q) AND {where} "
            f"ORDER BY distance ASC LIMIT :k"
        ),
        params,
    ).mappings().all()
    return [_row_to_retrieved_chunk(r, with_embedding) for r in rows]


def _row_to_retrieved_chunk(r, with_embedding: bool) -> RetrievedChunk:
    emb = None
    if with_embedding and r.get("embedding") is not None:
        raw = r["embedding"]
        if isinstance(raw, str):
            # pgvector returns "[v1,v2,...]" as string when no type adapter is set
            emb = [float(x) for x in raw.strip("[]").split(",") if x.strip()]
        else:
            emb = [float(x) for x in raw]
    return RetrievedChunk(
        chunk_id=int(r["id"]),
        section_id=int(r["section_id"]) if r["section_id"] is not None else None,
        section_path=r["section_path"],
        text=r["text"],
        distance=float(r["distance"]),
        order_idx=int(r["order_idx"]),
        embedding=emb,
    )


# ── ue2: tree-node ─────────────────────────────────────────────────────────

@dataclass
class TreeNode:
    id: int
    parent_id: int | None
    section_id: int | None
    level: int
    heading: str
    path: str
    order_idx: int
    summary: str
    text: str


def delete_ue2_tree(session: Session, document_id: int) -> None:
    session.execute(
        text("DELETE FROM ue2.tree_node WHERE document_id = :d"),
        {"d": document_id},
    )


def insert_tree_node(
    session: Session,
    *,
    document_id: int,
    parent_id: int | None,
    section_id: int | None,
    level: int,
    heading: str,
    path: str,
    order_idx: int,
    summary: str,
    text_: str,
) -> int:
    new_id = session.execute(
        text(
            "INSERT INTO ue2.tree_node "
            "(document_id, parent_id, section_id, level, heading, path, order_idx, summary, text) "
            "VALUES (:d, :p, :s, :l, :h, :path, :o, :sum, :txt) RETURNING id"
        ),
        {"d": document_id, "p": parent_id, "s": section_id, "l": level,
         "h": heading, "path": path, "o": order_idx, "sum": summary, "txt": text_},
    ).scalar()
    return int(new_id)


def ue2_tree_node_count(session: Session, document_id: int | None = None) -> int:
    if document_id is None:
        return int(session.execute(text("SELECT COUNT(*) FROM ue2.tree_node")).scalar() or 0)
    return int(
        session.execute(
            text("SELECT COUNT(*) FROM ue2.tree_node WHERE document_id = :d"),
            {"d": document_id},
        ).scalar()
        or 0
    )


def ue2_top_level_nodes(session: Session, document_id: int) -> list[TreeNode]:
    rows = session.execute(
        text(
            "SELECT id, parent_id, section_id, level, heading, path, order_idx, summary, text "
            "FROM ue2.tree_node "
            "WHERE document_id = :d AND parent_id IS NULL "
            "ORDER BY order_idx ASC"
        ),
        {"d": document_id},
    ).mappings().all()
    return [_row_to_tree_node(r) for r in rows]


def ue2_children_of(session: Session, parent_id: int) -> list[TreeNode]:
    rows = session.execute(
        text(
            "SELECT id, parent_id, section_id, level, heading, path, order_idx, summary, text "
            "FROM ue2.tree_node WHERE parent_id = :p ORDER BY order_idx ASC"
        ),
        {"p": parent_id},
    ).mappings().all()
    return [_row_to_tree_node(r) for r in rows]


# ── ue3: entity & community summaries ──────────────────────────────────────

def delete_ue3_summaries(session: Session) -> None:
    session.execute(text("TRUNCATE TABLE ue3.entity_summary, ue3.community_summary"))


def upsert_entity_summary(
    session: Session,
    *,
    entity_key: str,
    name: str,
    type_: str,
    description: str,
    mention_count: int,
    embedding: list[float],
) -> None:
    session.execute(
        text(
            "INSERT INTO ue3.entity_summary "
            "(entity_key, name, type, description, mention_count, embedding) "
            "VALUES (:k, :n, :t, :d, :m, :e) "
            "ON CONFLICT (entity_key) DO UPDATE SET "
            "  name = EXCLUDED.name, "
            "  type = EXCLUDED.type, "
            "  description = EXCLUDED.description, "
            "  mention_count = EXCLUDED.mention_count, "
            "  embedding = EXCLUDED.embedding"
        ),
        {"k": entity_key, "n": name, "t": type_, "d": description,
         "m": mention_count, "e": str(embedding)},
    )


def upsert_community_summary(
    session: Session,
    *,
    community_id: str,
    level: int,
    size: int,
    summary: str,
    entity_keys: list[str],
    embedding: list[float],
) -> None:
    session.execute(
        text(
            "INSERT INTO ue3.community_summary "
            "(community_id, level, size, summary, entity_keys, embedding) "
            "VALUES (:id, :l, :s, :sum, :keys, :e) "
            "ON CONFLICT (community_id) DO UPDATE SET "
            "  level = EXCLUDED.level, "
            "  size = EXCLUDED.size, "
            "  summary = EXCLUDED.summary, "
            "  entity_keys = EXCLUDED.entity_keys, "
            "  embedding = EXCLUDED.embedding"
        ),
        {"id": community_id, "l": level, "s": size, "sum": summary,
         "keys": entity_keys, "e": str(embedding)},
    )


def ue3_entity_count(session: Session) -> int:
    return int(session.execute(text("SELECT COUNT(*) FROM ue3.entity_summary")).scalar() or 0)


def ue3_community_count(session: Session) -> int:
    return int(session.execute(text("SELECT COUNT(*) FROM ue3.community_summary")).scalar() or 0)


@dataclass
class EntityMatch:
    entity_key: str
    name: str
    type: str
    description: str
    distance: float


def topk_entities_by_embedding(
    session: Session, query_embedding: Sequence[float], k: int,
) -> list[EntityMatch]:
    rows = session.execute(
        text(
            "SELECT entity_key, name, type, description, "
            "       embedding <=> CAST(:q AS vector) AS distance "
            "FROM ue3.entity_summary "
            "ORDER BY distance ASC LIMIT :k"
        ),
        {"q": str(list(query_embedding)), "k": k},
    ).mappings().all()
    return [
        EntityMatch(
            entity_key=r["entity_key"],
            name=r["name"],
            type=r["type"],
            description=r["description"],
            distance=float(r["distance"]),
        )
        for r in rows
    ]


def topk_entities_by_text(
    session: Session,
    keywords: Sequence[str],
    k: int,
    type_filter: Sequence[str] | None = None,
) -> list[EntityMatch]:
    """Case-insensitive substring search on entity name+description.
    Catches role/category words ("CEO", "Vorstand", "Gründer") that the
    embedding-based search misses when the query asks about a category but
    the graph stores instances (Steve Jobs, Tim Cook, …).

    Returns rows sorted by mention_count desc (most-referenced first) with a
    fixed pseudo-distance of 0.5 so the caller can blend with embedding hits.
    """
    if not keywords:
        return []
    params: dict[str, object] = {"k": k}
    name_conds = []
    desc_conds = []
    for i, w in enumerate(keywords):
        params[f"kw{i}"] = f"%{w}%"
        name_conds.append(f"name ILIKE :kw{i}")
        desc_conds.append(f"description ILIKE :kw{i}")
    where_keyword = "(" + " OR ".join(name_conds + desc_conds) + ")"
    where_type = ""
    if type_filter:
        types_csv = ", ".join(f":t{i}" for i in range(len(type_filter)))
        for i, t in enumerate(type_filter):
            params[f"t{i}"] = t
        where_type = f" AND type IN ({types_csv})"
    rows = session.execute(
        text(
            f"SELECT entity_key, name, type, description "
            f"FROM ue3.entity_summary "
            f"WHERE {where_keyword}{where_type} "
            f"ORDER BY mention_count DESC LIMIT :k"
        ),
        params,
    ).mappings().all()
    return [
        EntityMatch(
            entity_key=r["entity_key"],
            name=r["name"],
            type=r["type"],
            description=r["description"],
            distance=0.5,
        )
        for r in rows
    ]


@dataclass
class CommunityMatch:
    community_id: str
    level: int
    summary: str
    distance: float


def topk_communities_by_embedding(
    session: Session, query_embedding: Sequence[float], k: int,
) -> list[CommunityMatch]:
    rows = session.execute(
        text(
            "SELECT community_id, level, summary, "
            "       embedding <=> CAST(:q AS vector) AS distance "
            "FROM ue3.community_summary "
            "ORDER BY distance ASC LIMIT :k"
        ),
        {"q": str(list(query_embedding)), "k": k},
    ).mappings().all()
    return [
        CommunityMatch(
            community_id=r["community_id"],
            level=int(r["level"]),
            summary=r["summary"],
            distance=float(r["distance"]),
        )
        for r in rows
    ]


def latest_document_id_for_url(session: Session, url: str) -> int | None:
    """Walk raw → clean to find the document for the most recent snapshot."""
    row = session.execute(
        text(
            "SELECT cd.id FROM clean.document cd "
            "JOIN raw.wikipedia_snapshot s ON s.id = cd.snapshot_id "
            "WHERE s.url = :url "
            "ORDER BY s.fetched_at DESC LIMIT 1"
        ),
        {"url": url},
    ).scalar()
    return int(row) if row is not None else None


def _row_to_tree_node(r) -> TreeNode:
    return TreeNode(
        id=int(r["id"]),
        parent_id=int(r["parent_id"]) if r["parent_id"] is not None else None,
        section_id=int(r["section_id"]) if r["section_id"] is not None else None,
        level=int(r["level"]),
        heading=r["heading"],
        path=r["path"],
        order_idx=int(r["order_idx"]),
        summary=r["summary"],
        text=r["text"] or "",
    )


# ── meta ───────────────────────────────────────────────────────────────────

def start_ingest_run(session: Session, *, strategy: str, snapshot_id: int) -> int:
    new_id = session.execute(
        text(
            "INSERT INTO meta.ingest_run (strategy, snapshot_id, status) "
            "VALUES (:s, :sn, 'running') RETURNING id"
        ),
        {"s": strategy, "sn": snapshot_id},
    ).scalar()
    return int(new_id)


def finish_ingest_run(
    session: Session,
    run_id: int,
    *,
    status: str,
    stats: dict | None = None,
    error: str | None = None,
) -> None:
    session.execute(
        text(
            "UPDATE meta.ingest_run "
            "SET finished_at = NOW(), status = :st, stats = :stats, error = :err "
            "WHERE id = :id"
        ),
        {"st": status, "stats": json.dumps(stats or {}), "err": error, "id": run_id},
    )


def latest_ingest_run(session: Session, strategy: str) -> dict | None:
    row = session.execute(
        text(
            "SELECT id, strategy, snapshot_id, started_at, finished_at, status, stats, error "
            "FROM meta.ingest_run "
            "WHERE strategy = :s "
            "ORDER BY started_at DESC LIMIT 1"
        ),
        {"s": strategy},
    ).mappings().first()
    return dict(row) if row else None


def ingest_run(session: Session, run_id: int) -> dict | None:
    row = session.execute(
        text(
            "SELECT id, strategy, snapshot_id, started_at, finished_at, status, stats, error "
            "FROM meta.ingest_run WHERE id = :id"
        ),
        {"id": run_id},
    ).mappings().first()
    return dict(row) if row else None
