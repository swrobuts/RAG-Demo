"""UE1 — Simple RAG, now backed by the shared hybrid pipeline.

End user sees the same interface (top-k chunks + trace). Under the hood, every
query runs Dense + BM25 → RRF → LLM-rerank → MMR. UE2 calls the same pipeline
with a subtree filter from PageIndex.
"""
from __future__ import annotations

from backend.config import get_settings
from backend.retrieval.base import Chunk, RetrievalResult, SourceRef
from backend.retrieval.pipeline import hybrid_retrieve


class SimpleRAG:
    name = "ue1"

    def __init__(self, llm_provider: str = "gemini") -> None:
        # Reranker uses the same chat LLM the user picked (gemini or local).
        self._llm_provider = llm_provider

    def retrieve(self, query: str, k: int | None = None) -> RetrievalResult:
        settings = get_settings()
        k = k or settings.ue1_top_k
        result = hybrid_retrieve(
            query=query,
            k_final=k,
            section_paths=None,           # full corpus
            llm_provider=self._llm_provider,
        )
        chunks = [
            Chunk(text=r.text, section_path=r.section_path, chunk_id=r.chunk_id)
            for r in result.chunks
        ]
        sources = [
            SourceRef(
                chunk_id=r.chunk_id,
                section_path=r.section_path,
                text=r.text,
                distance=r.distance,
            )
            for r in result.chunks
        ]
        trace = {"strategy": self.name, "k": k, **result.trace}
        return RetrievalResult(chunks=chunks, sources=sources, trace=trace)
