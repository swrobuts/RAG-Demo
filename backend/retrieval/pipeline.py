"""Shared retrieval pipeline for every UE strategy.

Stages:

1. **Dense retrieval** (pgvector cosine) — top ``initial_k`` candidates.
2. **BM25 retrieval** (Postgres FTS, German stemming) — top ``initial_k``.
3. **Reciprocal Rank Fusion** — merge the two ranked lists into one.
4. **LLM rerank** — Gemini gives each candidate a 0–10 relevance score.
5. **MMR** — diversify the final selection (Maximum Marginal Relevance).

Both UE1 (no subtree filter) and UE2 (subtree filter from PageIndex) call
into this — UE2 just passes ``section_paths``. UE3 will plug in here too.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Sequence

from backend.config import get_settings
from backend.data import repo
from backend.data.pg import session_scope
from backend.llm.factory import get_chat_llm, get_embedding_llm

log = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    chunks: list[repo.RetrievedChunk]
    trace: dict = field(default_factory=dict)


def reciprocal_rank_fusion(
    ranked_lists: list[list[repo.RetrievedChunk]],
    rrf_k: int,
) -> list[repo.RetrievedChunk]:
    """Combine ranked lists by RRF: score(d) = Σ 1/(k + rank_i(d))."""
    scores: dict[int, float] = {}
    by_id: dict[int, repo.RetrievedChunk] = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked):
            scores[item.chunk_id] = scores.get(item.chunk_id, 0.0) + 1.0 / (rrf_k + rank)
            # Prefer the row that has an embedding (for MMR later).
            existing = by_id.get(item.chunk_id)
            if existing is None or (existing.embedding is None and item.embedding is not None):
                by_id[item.chunk_id] = item
    sorted_ids = sorted(scores.keys(), key=lambda cid: -scores[cid])
    return [by_id[cid] for cid in sorted_ids]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(x * x for x in b))
    if da == 0.0 or db == 0.0:
        return 0.0
    return num / (da * db)


def mmr_diversify(
    candidates: list[repo.RetrievedChunk],
    query_embedding: Sequence[float],
    k: int,
    lambda_: float,
) -> list[repo.RetrievedChunk]:
    """Greedy MMR. ``candidates`` should be ordered by relevance; rows without
    an embedding are kept in their input order at the end as a safety net."""
    with_emb = [c for c in candidates if c.embedding]
    without_emb = [c for c in candidates if not c.embedding]
    if not with_emb:
        return candidates[:k]

    # Precompute relevance to query (cosine similarity with query embedding).
    rel = {c.chunk_id: _cosine(c.embedding or [], query_embedding) for c in with_emb}

    selected: list[repo.RetrievedChunk] = []
    remaining = list(with_emb)
    while remaining and len(selected) < k:
        if not selected:
            best = max(remaining, key=lambda c: rel[c.chunk_id])
        else:
            def score(c: repo.RetrievedChunk) -> float:
                max_sim_to_selected = max(
                    _cosine(c.embedding or [], s.embedding or []) for s in selected
                )
                return lambda_ * rel[c.chunk_id] - (1.0 - lambda_) * max_sim_to_selected
            best = max(remaining, key=score)
        selected.append(best)
        remaining.remove(best)

    if len(selected) < k and without_emb:
        selected.extend(without_emb[: k - len(selected)])
    return selected


_RERANK_SYSTEM = (
    "Du bist ein RAG-Reranker. Du bekommst eine Frage und eine Liste "
    "nummerierter Auszüge. Bewerte jeden Auszug auf einer Skala von 0 "
    "(nicht relevant) bis 10 (perfekt relevant). Antworte AUSSCHLIESSLICH "
    "mit einem JSON-Array der Scores in der Reihenfolge der Auszüge, ohne "
    "Codefence, ohne Erklärung. Beispiel: [8, 3, 10, 0, 5]"
)


_JSON_ARRAY_RE = re.compile(r"\[\s*(?:-?\d+(?:\.\d+)?\s*(?:,\s*-?\d+(?:\.\d+)?\s*)*)?\]")


def _rerank_with_llm(
    query: str,
    candidates: list[repo.RetrievedChunk],
    llm_provider: str,
) -> list[float]:
    """Return a relevance score per candidate; falls back to a neutral 5.0
    if the LLM response can't be parsed."""
    listing_lines = []
    for i, c in enumerate(candidates, start=1):
        path = c.section_path or "?"
        # Truncate chunk to keep prompt tight
        snippet = (c.text or "")[:600].replace("\n", " ")
        listing_lines.append(f"{i}. [{path}] {snippet}")
    prompt = (
        f"Frage:\n{query}\n\n"
        f"Auszüge:\n" + "\n".join(listing_lines) + "\n\n"
        f"Antworte mit JSON-Array von {len(candidates)} Scores (0–10)."
    )
    try:
        chat = get_chat_llm(llm_provider)
        raw, _ = chat.generate(_RERANK_SYSTEM, prompt)
    except Exception as exc:  # noqa: BLE001
        log.warning("Reranker LLM failed: %s — keeping RRF order", exc)
        return [5.0] * len(candidates)
    m = _JSON_ARRAY_RE.search(raw or "")
    if not m:
        log.info("Reranker returned unparseable response: %r", (raw or "")[:200])
        return [5.0] * len(candidates)
    try:
        scores = json.loads(m.group(0))
    except json.JSONDecodeError:
        return [5.0] * len(candidates)
    out: list[float] = []
    for i in range(len(candidates)):
        v = scores[i] if i < len(scores) else 5.0
        try:
            out.append(max(0.0, min(10.0, float(v))))
        except (TypeError, ValueError):
            out.append(5.0)
    return out


def hybrid_retrieve(
    *,
    query: str,
    k_final: int,
    section_paths: Sequence[str] | None = None,
    llm_provider: str = "gemini",
) -> PipelineResult:
    """Run the full Dense → BM25 → RRF → LLM-rerank → MMR pipeline.

    ``section_paths`` non-empty → restrict both dense and BM25 to subtrees
    (UE2's PageIndex pre-filter). Empty/None → search the whole corpus (UE1).
    """
    settings = get_settings()
    initial_k = settings.pipeline_initial_k
    rrf_k = settings.pipeline_rrf_k

    embedder = get_embedding_llm()
    t0 = time.perf_counter()
    query_emb = embedder.embed([query])[0]
    embed_ms = (time.perf_counter() - t0) * 1000

    # ── Phase 1+2: Dense and BM25 retrieval, with embeddings (needed for MMR) ──
    t0 = time.perf_counter()
    with session_scope() as session:
        if section_paths:
            dense = repo.topk_ue1_in_subtree(
                session, query_emb, initial_k, section_paths, with_embedding=True,
            )
            sparse = repo.topk_ue1_bm25_in_subtree(
                session, query, initial_k, section_paths, with_embedding=True,
            )
        else:
            dense = repo.topk_ue1(session, query_emb, initial_k, with_embedding=True)
            sparse = repo.topk_ue1_bm25(session, query, initial_k, with_embedding=True)
    retrieve_ms = (time.perf_counter() - t0) * 1000

    # ── Phase 3: RRF fusion ──
    fused = reciprocal_rank_fusion([dense, sparse], rrf_k=rrf_k)

    # ── Phase 4: LLM rerank (optional) ──
    rerank_top_n = settings.pipeline_rerank_top_n
    reranked = fused[:rerank_top_n]
    rerank_ms = 0.0
    rerank_scores: list[float] | None = None
    if settings.pipeline_rerank_enabled and reranked:
        t0 = time.perf_counter()
        rerank_scores = _rerank_with_llm(query, reranked, llm_provider)
        rerank_ms = (time.perf_counter() - t0) * 1000
        # Reorder reranked list by score descending; keep stable tiebreak.
        idx_order = sorted(
            range(len(reranked)),
            key=lambda i: (-rerank_scores[i], i),
        )
        reranked = [reranked[i] for i in idx_order]
        rerank_scores = [rerank_scores[i] for i in idx_order]

    # ── Phase 5: MMR diversify ──
    if settings.pipeline_mmr_enabled and reranked:
        final = mmr_diversify(
            reranked, query_emb, k=k_final,
            lambda_=settings.pipeline_mmr_lambda,
        )
    else:
        final = reranked[:k_final]

    trace = {
        "pipeline": {
            "embed_ms": round(embed_ms, 1),
            "retrieve_ms": round(retrieve_ms, 1),
            "rerank_ms": round(rerank_ms, 1),
            "dense_count": len(dense),
            "bm25_count": len(sparse),
            "rrf_input_size": len(fused),
            "rerank_input_size": rerank_top_n,
            "rerank_enabled": settings.pipeline_rerank_enabled,
            "mmr_enabled": settings.pipeline_mmr_enabled,
            "mmr_lambda": settings.pipeline_mmr_lambda,
            "k_final": len(final),
        },
        "dense_top": [
            {"chunk_id": c.chunk_id, "section": c.section_path, "distance": round(c.distance, 4)}
            for c in dense[:8]
        ],
        "bm25_top": [
            {"chunk_id": c.chunk_id, "section": c.section_path, "distance": round(c.distance, 4)}
            for c in sparse[:8]
        ],
        "rerank_scores": rerank_scores if rerank_scores else None,
        "final_sections": [c.section_path for c in final],
    }
    return PipelineResult(chunks=final, trace=trace)
