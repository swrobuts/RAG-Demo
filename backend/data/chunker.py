"""Section-aware chunking with a hard cap on token count and sentence-safe splits.

Chunks never cross section boundaries — that's what makes the chunks
*explainable* (each chunk has a single section path), and it's the property
UE2 (PageIndex) and UE3 (GraphRAG) build on top of.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import tiktoken

_ENCODER = tiktoken.get_encoding("cl100k_base")


@dataclass
class Chunk:
    section_path: str
    order_idx: int
    text: str
    token_count: int


def _count_tokens(text: str) -> int:
    return len(_ENCODER.encode(text))


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9])")


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_RE.split(text) if p.strip()]
    return parts or [text.strip()]


def chunk_section(
    section_path: str,
    text: str,
    *,
    max_tokens: int,
    overlap_tokens: int,
    start_idx: int = 0,
) -> list[Chunk]:
    """Split a section's text into token-bounded, sentence-aligned chunks."""
    text = text.strip()
    if not text:
        return []
    sentences = _split_sentences(text)
    chunks: list[Chunk] = []
    cur: list[str] = []
    cur_tokens = 0
    idx = start_idx

    def flush() -> None:
        nonlocal cur, cur_tokens, idx
        if not cur:
            return
        body = " ".join(cur).strip()
        chunks.append(Chunk(
            section_path=section_path,
            order_idx=idx,
            text=body,
            token_count=_count_tokens(body),
        ))
        idx += 1

    for sent in sentences:
        st = _count_tokens(sent)
        if st > max_tokens:
            # Sentence alone exceeds the budget — split by tokens directly.
            flush()
            ids = _ENCODER.encode(sent)
            for i in range(0, len(ids), max_tokens - overlap_tokens):
                window = ids[i:i + max_tokens]
                body = _ENCODER.decode(window)
                chunks.append(Chunk(
                    section_path=section_path,
                    order_idx=idx,
                    text=body,
                    token_count=len(window),
                ))
                idx += 1
            continue
        if cur_tokens + st > max_tokens:
            flush()
            if overlap_tokens > 0 and chunks:
                tail_ids = _ENCODER.encode(chunks[-1].text)[-overlap_tokens:]
                tail = _ENCODER.decode(tail_ids)
                cur = [tail]
                cur_tokens = len(tail_ids)
            else:
                cur = []
                cur_tokens = 0
        cur.append(sent)
        cur_tokens += st

    flush()
    return chunks
