from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Protocol


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
        )


class ChatLLM(Protocol):
    name: str

    def generate(self, system: str, user: str) -> tuple[str, TokenUsage]: ...

    def stream(self, system: str, user: str) -> Iterator[str]: ...


class EmbeddingLLM(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...
