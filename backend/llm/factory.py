from __future__ import annotations

from functools import lru_cache

from backend.llm.base import ChatLLM, EmbeddingLLM
from backend.llm.gemini import GeminiChat, GeminiEmbedder
from backend.llm.lmstudio import LMStudioChat


def get_chat_llm(provider: str) -> ChatLLM:
    if provider == "gemini":
        return GeminiChat()
    if provider == "local":
        return LMStudioChat()
    raise ValueError(f"Unknown chat provider: {provider!r}")


@lru_cache
def get_embedding_llm() -> EmbeddingLLM:
    # Embeddings are always Gemini so the vector(768) columns stay consistent.
    return GeminiEmbedder()
