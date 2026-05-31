"""Gemini chat + embedding via the official ``google-genai`` SDK."""
from __future__ import annotations

import math
from typing import Iterator

from google import genai
from google.genai import types

from backend.config import get_settings
from backend.llm.base import TokenUsage


def _http_options():
    """Per-request timeout so a hanging Gemini call can't stall the whole
    ingest pipeline. Returns None if the SDK version doesn't support timeout
    via HttpOptions — caller falls back to default. The thread-watchdog in
    ue3_graphrag._generate_with_timeout() provides a backstop either way."""
    cls = getattr(types, "HttpOptions", None)
    if cls is None:
        return None
    try:
        return cls(timeout=60_000)  # 60s hard cutoff per call
    except TypeError:
        return None


class GeminiChat:
    name = "gemini"

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        opts = _http_options()
        self._client = (
            genai.Client(api_key=settings.gemini_api_key, http_options=opts)
            if opts is not None
            else genai.Client(api_key=settings.gemini_api_key)
        )
        self._model = settings.gemini_chat_model

    def _config(self, system: str) -> types.GenerateContentConfig:
        settings = get_settings()
        return types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=settings.max_tokens_per_call,
            temperature=0.2,
        )

    def generate(self, system: str, user: str) -> tuple[str, TokenUsage]:
        resp = self._client.models.generate_content(
            model=self._model,
            contents=user,
            config=self._config(system),
        )
        usage = TokenUsage(
            prompt_tokens=getattr(resp.usage_metadata, "prompt_token_count", 0) or 0,
            completion_tokens=getattr(resp.usage_metadata, "candidates_token_count", 0) or 0,
        )
        return (resp.text or "", usage)

    def stream(self, system: str, user: str) -> Iterator[str]:
        for chunk in self._client.models.generate_content_stream(
            model=self._model,
            contents=user,
            config=self._config(system),
        ):
            if chunk.text:
                yield chunk.text


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


class GeminiEmbedder:
    name = "gemini"

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        opts = _http_options()
        self._client = (
            genai.Client(api_key=settings.gemini_api_key, http_options=opts)
            if opts is not None
            else genai.Client(api_key=settings.gemini_api_key)
        )
        self._model = settings.gemini_embedding_model
        self.dim = settings.gemini_embedding_dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.models.embed_content(
            model=self._model,
            contents=texts,
        )
        # gemini-embedding-001 returns 3072 dims by default. We use Matryoshka
        # truncation down to ``self.dim`` (768) and re-normalise so cosine
        # similarity remains meaningful — Google explicitly recommends
        # normalising after truncation.
        out: list[list[float]] = []
        for e in resp.embeddings:
            full = list(e.values)
            truncated = full[: self.dim] if len(full) > self.dim else full
            out.append(_l2_normalize(truncated))
        return out
