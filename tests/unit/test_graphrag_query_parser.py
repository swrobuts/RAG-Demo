"""Tests for the query parsers used by UE3 GraphRAG."""
from backend.retrieval.graphrag import extract_keywords, extract_type_hints


def test_type_hint_person():
    assert "PERSON" in extract_type_hints("Wer waren alle CEOs von Apple?")
    assert "PERSON" in extract_type_hints("Welche Personen wirkten bei der Gründung?")
    assert "PERSON" in extract_type_hints("Wer war der Designer des iPhone?")


def test_type_hint_product():
    assert "PRODUCT" in extract_type_hints("Welche Produkte hat Apple entwickelt?")
    assert "PRODUCT" in extract_type_hints("Welche Geräte gibt es?")


def test_type_hint_none_for_vague_query():
    # No specific type-asking trigger word
    assert extract_type_hints("Erzähl mir über Apple") == []


def test_type_hint_multiple():
    hits = extract_type_hints("Welche Personen und welche Produkte sind in der Gründungsphase wichtig?")
    assert "PERSON" in hits
    assert "PRODUCT" in hits


def test_extract_keywords_drops_stopwords():
    kws = extract_keywords("Wer waren alle CEOs von Apple?")
    assert "ceos" in kws
    assert "apple" in kws
    assert "wer" not in kws
    assert "alle" not in kws
    assert "von" not in kws


def test_extract_keywords_filters_short_tokens():
    kws = extract_keywords("XY ist im Jahr 1976 CEO geworden")
    assert "ceo" in kws
    assert "jahr" in kws
    assert "geworden" in kws
    # short / numeric tokens are dropped
    assert "xy" not in kws
    assert "1976" not in kws


def test_extract_keywords_deduplicates_and_caps():
    kws = extract_keywords("Apple Apple Apple Konzept Konzept iPhone iPad Mac MacBook iMac Studio Watch")
    assert kws.count("apple") == 1
    assert len(kws) <= 8


def test_extract_keywords_lowercases():
    kws = extract_keywords("Steve Jobs gründete Apple Computer")
    assert "steve" in kws
    assert "apple" in kws
    assert "Steve" not in kws
