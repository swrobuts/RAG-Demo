from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Chunk:
    text: str
    section_path: str | None = None
    chunk_id: int | None = None


@dataclass
class SourceRef:
    chunk_id: int
    section_path: str | None
    text: str
    distance: float | None = None


@dataclass
class RetrievalResult:
    chunks: list[Chunk]
    sources: list[SourceRef]
    trace: dict = field(default_factory=dict)


class RetrievalStrategy(Protocol):
    name: str

    def retrieve(self, query: str, k: int = 8) -> RetrievalResult: ...
