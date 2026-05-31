"""UE2 ingest: build a PageIndex-style tree of LLM-written summaries.

The tree mirrors ``clean.section``. Summaries are produced bottom-up:

- Leaf node: Gemini summarises (heading + first ~1200 chars of body) in 2
  sentences.
- Internal node: Gemini summarises (heading + the children's summaries) in
  2 sentences — so each level's summary describes what the subtree covers.

At query time the navigator LLM only ever sees these summaries (not the raw
text), so they have to be informative on their own.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import text

from backend.config import get_settings
from backend.data import repo
from backend.data.pg import session_scope
from backend.ingest.common import ensure_clean_document
from backend.llm.factory import get_chat_llm

log = logging.getLogger(__name__)

LEAF_SYSTEM = (
    "Du fasst Abschnitte eines deutschen Wikipedia-Artikels für ein "
    "Retrieval-System zusammen. Antworte in maximal zwei Sätzen, faktenorientiert, "
    "ohne Floskeln, ohne Selbstbezug."
)
INTERNAL_SYSTEM = (
    "Du fasst die Gesamtaussage eines Abschnitts zusammen, basierend auf den "
    "Zusammenfassungen seiner Unterabschnitte. Maximal zwei Sätze, faktenorientiert, "
    "kennzeichne welche Themen der Abschnitt insgesamt abdeckt."
)
MAX_LEAF_TEXT_CHARS = 1200


@dataclass
class UE2IngestStats:
    document_id: int
    nodes: int
    leaves: int
    internal: int
    llm_calls: int
    duration_ms: float


@dataclass
class _SectionRow:
    id: int
    parent_id: int | None
    level: int
    heading: str
    path: str
    order_idx: int
    text: str


def _load_sections(document_id: int) -> list[_SectionRow]:
    with session_scope() as session:
        rows = session.execute(
            text(
                "SELECT id, parent_id, level, heading, path, order_idx, text "
                "FROM clean.section WHERE document_id = :d "
                "ORDER BY order_idx ASC"
            ),
            {"d": document_id},
        ).mappings().all()
    return [
        _SectionRow(
            id=int(r["id"]),
            parent_id=int(r["parent_id"]) if r["parent_id"] is not None else None,
            level=int(r["level"]),
            heading=r["heading"],
            path=r["path"],
            order_idx=int(r["order_idx"]),
            text=r["text"] or "",
        )
        for r in rows
    ]


def _leaf_summary(chat, sec: _SectionRow) -> str:
    body = sec.text.strip()[:MAX_LEAF_TEXT_CHARS]
    prompt = f"Abschnittspfad: {sec.path}\n\nText:\n{body}\n\nZusammenfassung:"
    answer, _ = chat.generate(LEAF_SYSTEM, prompt)
    return answer.strip() or sec.heading


def _internal_summary(chat, sec: _SectionRow, child_summaries: list[str]) -> str:
    children_block = "\n".join(f"- {s}" for s in child_summaries)
    prompt = (
        f"Abschnittspfad: {sec.path}\n\n"
        f"Zusammenfassungen der Unterabschnitte:\n{children_block}\n\n"
        f"Gesamtzusammenfassung:"
    )
    answer, _ = chat.generate(INTERNAL_SYSTEM, prompt)
    return answer.strip() or sec.heading


def run_ue2_ingest(force: bool = False) -> UE2IngestStats:
    settings = get_settings()
    t0 = time.perf_counter()

    clean = ensure_clean_document(force=force)
    document_id = clean.document_id

    with session_scope() as session:
        existing = repo.ue2_tree_node_count(session, document_id)
    if existing > 0 and not force:
        log.info("UE2 ingest: %d nodes already present — skipping (use force to rebuild)", existing)
        return UE2IngestStats(
            document_id=document_id,
            nodes=existing,
            leaves=0,
            internal=0,
            llm_calls=0,
            duration_ms=(time.perf_counter() - t0) * 1000,
        )

    sections = _load_sections(document_id)
    if not sections:
        raise RuntimeError("UE2 ingest: no sections found — run UE1 ingest first")

    log.info("UE2 ingest: %d sections to summarise (bottom-up)", len(sections))

    # Bucket children by parent for fast lookup.
    children_by_parent: dict[int | None, list[_SectionRow]] = {}
    for s in sections:
        children_by_parent.setdefault(s.parent_id, []).append(s)
    for v in children_by_parent.values():
        v.sort(key=lambda s: s.order_idx)

    # Topological order: deepest first. clean.section.order_idx already
    # follows document order, so the children of a node always have higher
    # order_idx than the node itself — sorting by depth descending then
    # order_idx ascending gives us a correct bottom-up sequence.
    by_depth_desc = sorted(sections, key=lambda s: (-s.level, s.order_idx))

    chat = get_chat_llm("gemini")  # ingest uses Gemini for consistency

    summaries: dict[int, str] = {}
    llm_calls = 0
    leaves = 0
    internal = 0

    for sec in by_depth_desc:
        kids = children_by_parent.get(sec.id, [])
        is_leaf = not kids
        if is_leaf:
            if not sec.text.strip():
                summaries[sec.id] = sec.heading
            else:
                summaries[sec.id] = _leaf_summary(chat, sec)
                llm_calls += 1
            leaves += 1
        else:
            child_summaries = [summaries[c.id] for c in kids if c.id in summaries]
            summaries[sec.id] = _internal_summary(chat, sec, child_summaries)
            llm_calls += 1
            internal += 1

    log.info("UE2 ingest: %d summaries done in %d LLM calls", len(summaries), llm_calls)

    # Persist tree (use the SAME id mapping as clean.section so parent_id
    # references resolve trivially, just within ue2.tree_node).
    id_map: dict[int, int] = {}  # clean.section.id → ue2.tree_node.id
    with session_scope() as session:
        repo.delete_ue2_tree(session, document_id)
        # In document order (parents always before children because the cleaner
        # walks the tree top-down).
        for sec in sorted(sections, key=lambda s: s.order_idx):
            parent_tn = id_map.get(sec.parent_id) if sec.parent_id is not None else None
            tn_id = repo.insert_tree_node(
                session,
                document_id=document_id,
                parent_id=parent_tn,
                section_id=sec.id,
                level=sec.level,
                heading=sec.heading,
                path=sec.path,
                order_idx=sec.order_idx,
                summary=summaries[sec.id],
                text_=sec.text,
            )
            session.flush()
            id_map[sec.id] = tn_id

    duration_ms = (time.perf_counter() - t0) * 1000
    log.info("UE2 ingest: done in %.1f ms", duration_ms)
    return UE2IngestStats(
        document_id=document_id,
        nodes=len(sections),
        leaves=leaves,
        internal=internal,
        llm_calls=llm_calls,
        duration_ms=duration_ms,
    )
