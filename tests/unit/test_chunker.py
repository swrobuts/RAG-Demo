from backend.data.chunker import chunk_section


def test_short_text_yields_single_chunk():
    chunks = chunk_section(
        "Test > Path",
        "Ein kurzer Satz. Noch einer.",
        max_tokens=400,
        overlap_tokens=50,
    )
    assert len(chunks) == 1
    assert chunks[0].section_path == "Test > Path"
    assert chunks[0].order_idx == 0
    assert chunks[0].token_count > 0


def test_empty_text_yields_no_chunks():
    assert chunk_section("p", "", max_tokens=400, overlap_tokens=50) == []
    assert chunk_section("p", "   ", max_tokens=400, overlap_tokens=50) == []


def test_long_text_splits_into_multiple_chunks_with_overlap():
    sentence = "Apple wurde 1976 von Steve Jobs, Steve Wozniak und Ronald Wayne gegründet. "
    text = sentence * 200
    chunks = chunk_section("Geschichte", text, max_tokens=80, overlap_tokens=20)
    assert len(chunks) > 1
    for c in chunks:
        assert c.token_count <= 80 + 5  # small slack for tokenizer edges
    assert all(c.order_idx == i for i, c in enumerate(chunks))


def test_order_idx_offsets_when_start_idx_given():
    chunks = chunk_section(
        "p", "Satz eins. Satz zwei.",
        max_tokens=400, overlap_tokens=0, start_idx=7,
    )
    assert chunks[0].order_idx == 7
