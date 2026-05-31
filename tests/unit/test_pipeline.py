"""Pure-Python tests for the retrieval pipeline pieces (RRF and MMR) — no
DB/LLM needed. The hybrid_retrieve() entry point is exercised via the
end-to-end runs on the VPS."""
from backend.data.repo import RetrievedChunk
from backend.retrieval.pipeline import (
    _cosine,
    mmr_diversify,
    reciprocal_rank_fusion,
)


def _chunk(cid: int, *, distance: float = 0.0, emb: list[float] | None = None,
           section: str = "Geschichte") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid,
        section_id=None,
        section_path=section,
        text=f"chunk {cid}",
        distance=distance,
        order_idx=cid,
        embedding=emb,
    )


# ── RRF ────────────────────────────────────────────────────────────────────

def test_rrf_combines_overlapping_lists():
    dense = [_chunk(1), _chunk(2), _chunk(3)]
    sparse = [_chunk(2), _chunk(4)]
    fused = reciprocal_rank_fusion([dense, sparse], rrf_k=60)
    # Chunk 2 is rank 2 in dense and rank 1 in sparse → highest combined score
    assert fused[0].chunk_id == 2
    # 1, 3, 4 follow; exact order depends on the curve but no duplicates
    ids = [c.chunk_id for c in fused]
    assert set(ids) == {1, 2, 3, 4}
    assert len(ids) == 4


def test_rrf_preserves_embedding_from_either_list():
    dense = [_chunk(1, emb=None), _chunk(2, emb=[0.1, 0.2])]
    sparse = [_chunk(1, emb=[0.3, 0.4])]   # same id, has embedding
    fused = reciprocal_rank_fusion([dense, sparse], rrf_k=60)
    c1 = next(c for c in fused if c.chunk_id == 1)
    assert c1.embedding == [0.3, 0.4]


def test_rrf_empty_lists_return_empty():
    assert reciprocal_rank_fusion([], rrf_k=60) == []
    assert reciprocal_rank_fusion([[], []], rrf_k=60) == []


# ── Cosine helper ──────────────────────────────────────────────────────────

def test_cosine_handles_zero_vector():
    assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert _cosine([1.0, 0.0], [0.0, 0.0]) == 0.0


def test_cosine_basic_values():
    assert abs(_cosine([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9
    assert abs(_cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9
    assert abs(_cosine([1.0, 1.0], [1.0, 1.0]) - 1.0) < 1e-9


# ── MMR ────────────────────────────────────────────────────────────────────

def test_mmr_picks_diverse_chunks_over_similar_top():
    query_emb = [1.0, 0.0]
    candidates = [
        _chunk(1, emb=[1.0, 0.0]),       # very relevant
        _chunk(2, emb=[0.99, 0.01]),     # near-duplicate of 1
        _chunk(3, emb=[0.6, 0.8]),       # diverse, still somewhat relevant
    ]
    # λ=0.3 weights diversity heavier than relevance — the redundant chunk 2
    # gets penalised hard, the diverse 3 wins. At exactly λ=0.5 the scores
    # are mathematically symmetric for this configuration (rel and
    # max_sim_to_c1 happen to be equal for every candidate when the query
    # equals c1's direction), so any picker is fine.
    selected = mmr_diversify(candidates, query_emb, k=2, lambda_=0.3)
    ids = [c.chunk_id for c in selected]
    assert ids[0] == 1
    assert ids[1] == 3


def test_mmr_falls_back_to_input_order_when_no_embeddings():
    query_emb = [1.0, 0.0]
    candidates = [_chunk(1), _chunk(2), _chunk(3)]
    selected = mmr_diversify(candidates, query_emb, k=2, lambda_=0.7)
    assert [c.chunk_id for c in selected] == [1, 2]


def test_mmr_returns_k_or_fewer():
    query_emb = [1.0, 0.0]
    candidates = [_chunk(i, emb=[1.0, 0.0]) for i in range(3)]
    assert len(mmr_diversify(candidates, query_emb, k=8, lambda_=0.7)) == 3


def test_mmr_high_lambda_emphasises_relevance():
    """λ → 1 should rank purely by relevance even when redundancy is high."""
    query_emb = [1.0, 0.0]
    candidates = [
        _chunk(1, emb=[1.0, 0.0]),
        _chunk(2, emb=[0.99, 0.01]),    # redundant but still top-relevant
        _chunk(3, emb=[0.5, 0.5]),       # diverse but less relevant
    ]
    selected = mmr_diversify(candidates, query_emb, k=2, lambda_=1.0)
    assert [c.chunk_id for c in selected] == [1, 2]
