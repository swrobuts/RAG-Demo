"""UE1 ingest: builds on the shared ``ensure_clean_document`` and adds
section-aware chunks with Gemini embeddings to ``ue1.chunk``."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from backend.config import get_settings
from backend.data import repo
from backend.data.chunker import Chunk, chunk_section
from backend.data.pg import session_scope
from backend.ingest.common import ensure_clean_document
from backend.llm.factory import get_embedding_llm

log = logging.getLogger(__name__)


@dataclass
class UE1IngestStats:
    snapshot_created: bool
    document_id: int
    sections: int
    chunks: int
    duration_ms: float


def run_ue1_ingest(force: bool = False) -> UE1IngestStats:
    settings = get_settings()
    t0 = time.perf_counter()

    clean = ensure_clean_document(force=force)

    # Skip-shortcut: snapshot unchanged + chunks already present.
    if not clean.document_created and not force:
        with session_scope() as session:
            n = repo.ue1_chunk_count(session, clean.document_id)
            if n > 0:
                log.info("UE1 ingest: snapshot unchanged and %d chunks present — skipping", n)
                return UE1IngestStats(
                    snapshot_created=clean.snapshot_created,
                    document_id=clean.document_id,
                    sections=0,
                    chunks=n,
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )

    # ── ue1.chunk ──
    with session_scope() as session:
        repo.delete_ue1_chunks(session, clean.document_id)

    all_chunks: list[tuple[int, Chunk]] = []
    chunk_idx = 0
    for section_id, path, text_ in clean.flat_sections:
        chunks = chunk_section(
            section_path=path,
            text=text_,
            max_tokens=settings.ue1_chunk_tokens,
            overlap_tokens=settings.ue1_chunk_overlap,
            start_idx=chunk_idx,
        )
        for c in chunks:
            all_chunks.append((section_id, c))
        chunk_idx += len(chunks)
    log.info("UE1 ingest: produced %d chunks, embedding via Gemini", len(all_chunks))

    # Embed (section path + chunk text) instead of bare chunk text. The
    # embedding model gets crucial context — same chunk wording can mean very
    # different things under "Geschichte > Gründung" vs "Produkte > iPhone".
    # Costs nothing extra; gains a substantial chunk of retrieval quality.
    embedder = get_embedding_llm()
    BATCH = 100
    embeddings: list[list[float]] = []
    for i in range(0, len(all_chunks), BATCH):
        batch_texts: list[str] = []
        for _section_id, chunk in all_chunks[i:i + BATCH]:
            prefix = f"Sektion: {chunk.section_path}\n\n" if chunk.section_path else ""
            batch_texts.append(prefix + chunk.text)
        embeddings.extend(embedder.embed(batch_texts))

    with session_scope() as session:
        for (section_id, chunk), embedding in zip(all_chunks, embeddings):
            repo.insert_ue1_chunk(
                session,
                document_id=clean.document_id,
                section_id=section_id,
                order_idx=chunk.order_idx,
                chunk_text=chunk.text,
                token_count=chunk.token_count,
                embedding=embedding,
            )

    duration_ms = (time.perf_counter() - t0) * 1000
    log.info("UE1 ingest: done in %.1f ms", duration_ms)
    return UE1IngestStats(
        snapshot_created=clean.snapshot_created,
        document_id=clean.document_id,
        sections=len(clean.flat_sections),
        chunks=len(all_chunks),
        duration_ms=duration_ms,
    )
