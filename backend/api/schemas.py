from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Strategy = Literal["ue1", "ue2", "ue3", "ue4"]
LLMProvider = Literal["gemini", "local"]


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    strategy: Strategy = "ue1"
    llm: LLMProvider = "gemini"
    k: int | None = Field(default=None, ge=1, le=32)


class IngestRequest(BaseModel):
    strategy: Strategy = "ue1"
    force: bool = False


class SourcePayload(BaseModel):
    chunk_id: int | None
    section_path: str | None
    text: str
    distance: float | None = None


class QueryResponse(BaseModel):
    answer: str
    sources: list[SourcePayload]
    trace: dict
    llm: LLMProvider
    strategy: Strategy
    latency_ms: float


class CompareRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    strategies: list[Strategy] = Field(default_factory=lambda: ["ue1", "ue2"])
    llm: LLMProvider = "gemini"
    k: int | None = Field(default=None, ge=1, le=32)


class StrategyResult(BaseModel):
    strategy: Strategy
    answer: str
    sources: list[SourcePayload]
    trace: dict
    latency_ms: float
    llm_calls: int
    token_usage: dict
    skipped_llm: bool = False


class CompareResponse(BaseModel):
    query: str
    llm: LLMProvider
    results: list[StrategyResult]
    evaluation: dict
    total_latency_ms: float
