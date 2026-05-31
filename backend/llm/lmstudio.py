"""Local chat via LM Studio's OpenAI-compatible endpoint."""
from __future__ import annotations

from typing import Iterator

from openai import OpenAI

from backend.config import get_settings
from backend.llm.base import TokenUsage


class LMStudioChat:
    name = "local"

    def __init__(self) -> None:
        settings = get_settings()
        # LM Studio doesn't enforce an API key; ``not-needed`` is the convention.
        self._client = OpenAI(base_url=settings.local_llm_url, api_key="not-needed")
        self._model = settings.local_llm_model
        self._max_tokens = settings.max_tokens_per_call

    def generate(self, system: str, user: str) -> tuple[str, TokenUsage]:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=self._max_tokens,
            temperature=0.2,
        )
        usage = TokenUsage(
            prompt_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
        )
        return (resp.choices[0].message.content or "", usage)

    def stream(self, system: str, user: str) -> Iterator[str]:
        stream = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=self._max_tokens,
            temperature=0.2,
            stream=True,
        )
        for event in stream:
            delta = event.choices[0].delta.content if event.choices else None
            if delta:
                yield delta
