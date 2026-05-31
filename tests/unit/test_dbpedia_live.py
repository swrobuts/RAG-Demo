"""Tests for the live-DBpedia fallback (third-tier UE4 retrieval)."""
from unittest.mock import patch

from backend.retrieval import dbpedia_live as dl


# ── extract_anchors ───────────────────────────────────────────────────────

def test_extract_anchors_pulls_literals_from_sparql():
    sparql = '''
    SELECT ?n WHERE {
      ?x rdfs:label "PowerBook 145b"@en .
      ?pred apple:predecessorOf ?x .
    }
    '''
    assert dl.extract_anchors(sparql) == ["PowerBook 145b"]


def test_extract_anchors_handles_multiple_unique_literals():
    sparql = '''
    SELECT ?n WHERE {
      ?a rdfs:label "iPhone 4"@en .
      ?b rdfs:label "iPhone 5"@en .
    }
    '''
    assert dl.extract_anchors(sparql) == ["iPhone 4", "iPhone 5"]


def test_extract_anchors_dedups():
    """Same label appearing multiple times with different lang tags →
    one entry only."""
    sparql = '"iPhone 4"@en "iPhone 4"@de "iPhone 4"'
    assert dl.extract_anchors(sparql) == ["iPhone 4"]


def test_extract_anchors_ignores_short_labels():
    sparql = '"a"@en "XX"@en "iPhone"@en'
    assert dl.extract_anchors(sparql) == ["iPhone"]


def test_extract_anchors_empty_on_empty_sparql():
    assert dl.extract_anchors("") == []
    assert dl.extract_anchors(None) == []  # type: ignore[arg-type]


# ── lookup_entity ─────────────────────────────────────────────────────────

def test_lookup_entity_returns_empty_for_short_label():
    assert dl.lookup_entity("") == []
    assert dl.lookup_entity("a") == []


def test_lookup_entity_returns_empty_on_dbpedia_failure():
    with patch.object(dl, "_dbpedia_query", return_value={}):
        assert dl.lookup_entity("PowerBook 145b") == []


def test_lookup_entity_parses_predecessor_and_successor():
    fake = {"results": {"bindings": [
        {"s":     {"value": "http://dbpedia.org/resource/IPhone_4"},
         "l":     {"value": "iPhone 4"},
         "pred":  {"value": "http://dbpedia.org/resource/IPhone_3GS"},
         "predLabel": {"value": "iPhone 3GS"},
         "succ":  {"value": "http://dbpedia.org/resource/IPhone_4S"},
         "succLabel": {"value": "iPhone 4S"}},
    ]}}
    with patch.object(dl, "_dbpedia_query", return_value=fake):
        out = dl.lookup_entity("iPhone 4")
    assert len(out) == 1
    assert out[0]["label"] == "iPhone 4"
    assert out[0]["predecessor_label"] == "iPhone 3GS"
    assert out[0]["successor_label"] == "iPhone 4S"


def test_lookup_entity_coalesces_multiple_rows_per_entity():
    """OPTIONAL queries can yield multiple rows per subject."""
    fake = {"results": {"bindings": [
        {"s": {"value": "x"}, "l": {"value": "Foo"},
         "pred": {"value": "p1"}, "predLabel": {"value": "P1"}},
        {"s": {"value": "x"}, "l": {"value": "Foo"},
         "succ": {"value": "s1"}, "succLabel": {"value": "S1"}},
    ]}}
    with patch.object(dl, "_dbpedia_query", return_value=fake):
        out = dl.lookup_entity("Foo")  # label must be ≥3 chars
    assert len(out) == 1
    assert out[0]["predecessor_label"] == "P1"
    assert out[0]["successor_label"] == "S1"


# ── lookup_to_chunks ──────────────────────────────────────────────────────

def test_lookup_to_chunks_formats_facts_as_readable_text():
    fake_entries = [{
        "uri": "http://dbpedia.org/resource/IPhone_4",
        "label": "iPhone 4",
        "comment": "The iPhone 4 is a smartphone …",
        "predecessor": "http://dbpedia.org/resource/IPhone_3GS",
        "predecessor_label": "iPhone 3GS",
        "successor": "http://dbpedia.org/resource/IPhone_4S",
        "successor_label": "iPhone 4S",
        "manufacturer": "http://dbpedia.org/resource/Apple_Inc.",
        "manufacturer_label": "Apple Inc.",
    }]
    with patch.object(dl, "extract_anchors", return_value=["iPhone 4"]), \
         patch.object(dl, "lookup_entity", return_value=fake_entries):
        chunks, sources = dl.lookup_to_chunks('"iPhone 4"@en')
    assert len(chunks) == 1
    txt = chunks[0].text
    assert "iPhone 4" in txt
    assert "Vorgänger: iPhone 3GS" in txt
    assert "Nachfolger: iPhone 4S" in txt
    assert "Hersteller: Apple Inc." in txt
    assert "Beschreibung:" in txt
    assert chunks[0].section_path == "DBpedia · live · iPhone 4"
    assert len(sources) == 1


def test_lookup_to_chunks_empty_when_no_anchors():
    with patch.object(dl, "extract_anchors", return_value=[]):
        chunks, sources = dl.lookup_to_chunks("")
    assert chunks == [] and sources == []
