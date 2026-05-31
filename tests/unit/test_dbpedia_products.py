"""Tests for the DBpedia products enrichment.

HTTP (DBpedia) and graph writes (GraphDB) are mocked. We test the
parsing logic, the SPARQL strings generated, and the orchestration
sequencing — not the live endpoints."""
from unittest.mock import patch

from backend.ingest import dbpedia_products as dp


# ── Helpers ───────────────────────────────────────────────────────────────

def test_slugify_strips_diacritics_and_pascal_cases():
    assert dp._slugify("PowerBook 100 series") == "PowerBook100Series"
    assert dp._slugify("Macintosh SE/30") == "MacintoshSE30"
    assert dp._slugify("iPhone 4s") == "IPhone4s"
    assert dp._slugify("  ") == "Unknown"


def test_escape_literal_handles_quotes_and_newlines():
    assert dp._escape_literal('hello "world"') == 'hello \\"world\\"'
    assert dp._escape_literal("line\nbreak") == "line break"


# ── DBpedia fetch ─────────────────────────────────────────────────────────

def test_fetch_apple_products_coalesces_multiple_rows_per_product():
    """DBpedia returns one row per (product, predecessor, successor)
    tuple — we should coalesce into one record per product."""
    fake = {
        "results": {"bindings": [
            {"product": {"value": "http://dbpedia.org/resource/IPhone_4"},
             "label":   {"value": "iPhone 4"},
             "predecessor": {"value": "http://dbpedia.org/resource/IPhone_3GS"},
             "predLabel":   {"value": "iPhone 3GS"},
             "successor": {"value": "http://dbpedia.org/resource/IPhone_4S"},
             "succLabel":   {"value": "iPhone 4S"}},
            # A second row for the same product — should not duplicate.
            {"product": {"value": "http://dbpedia.org/resource/IPhone_4"},
             "label":   {"value": "iPhone 4"},
             "predecessor": {"value": "http://dbpedia.org/resource/IPhone_3GS"},
             "predLabel":   {"value": "iPhone 3GS"}},
        ]}
    }
    with patch.object(dp, "_dbpedia_query", return_value=fake):
        out = dp.fetch_apple_products()
    assert len(out) == 1
    assert out[0]["name"] == "iPhone 4"
    assert out[0]["predecessor_name"] == "iPhone 3GS"
    assert out[0]["successor_name"] == "iPhone 4S"


def test_fetch_apple_products_keeps_product_without_chronology():
    fake = {
        "results": {"bindings": [
            {"product": {"value": "http://dbpedia.org/resource/Macintosh"},
             "label":   {"value": "Macintosh"}},
        ]}
    }
    with patch.object(dp, "_dbpedia_query", return_value=fake):
        out = dp.fetch_apple_products()
    assert len(out) == 1
    assert out[0]["predecessor"] is None
    assert out[0]["successor"] is None


def test_fetch_apple_products_empty_on_dbpedia_failure():
    with patch.object(dp, "_dbpedia_query", return_value={}):
        assert dp.fetch_apple_products() == []


# ── Insert ────────────────────────────────────────────────────────────────

def test_insert_product_skips_when_already_present():
    p = {"uri": "http://dbpedia.org/resource/IPhone_4",
         "name": "iPhone 4",
         "predecessor": None, "predecessor_name": None,
         "successor": None,   "successor_name": None}
    with patch.object(dp, "_product_exists", return_value=True), \
         patch.object(dp.graphdb_client, "update") as mock_update:
        added = dp.insert_product(p)
        mock_update.assert_not_called()
    assert added == {"product": 0, "successor": 0, "predecessor": 0}


def test_insert_product_writes_product_and_successor_triples():
    captured = {}
    def _capture(s): captured["sparql"] = s
    p = {"uri": "http://dbpedia.org/resource/IPhone_4",
         "name": "iPhone 4",
         "predecessor": None, "predecessor_name": None,
         "successor": "http://dbpedia.org/resource/IPhone_4S",
         "successor_name": "iPhone 4S"}
    with patch.object(dp, "_product_exists", return_value=False), \
         patch.object(dp.graphdb_client, "update", side_effect=_capture):
        added = dp.insert_product(p)
    assert added["product"] == 1
    assert added["successor"] == 1
    assert added["predecessor"] == 0
    s = captured["sparql"]
    # Product subject + label + sameAs
    assert "apple:IPhone4 a apple:Product" in s
    assert 'rdfs:label "iPhone 4"@en' in s
    assert "<http://dbpedia.org/resource/IPhone_4>" in s
    # Successor relation triple
    assert "apple:IPhone4 apple:successorOf apple:IPhone4S" in s


def test_insert_product_with_both_predecessor_and_successor():
    captured = {}
    def _capture(s): captured["sparql"] = s
    p = {"uri": "http://dbpedia.org/resource/IPhone_4",
         "name": "iPhone 4",
         "predecessor": "http://dbpedia.org/resource/IPhone_3GS",
         "predecessor_name": "iPhone 3GS",
         "successor": "http://dbpedia.org/resource/IPhone_4S",
         "successor_name": "iPhone 4S"}
    with patch.object(dp, "_product_exists", return_value=False), \
         patch.object(dp.graphdb_client, "update", side_effect=_capture):
        added = dp.insert_product(p)
    s = captured["sparql"]
    assert "apple:IPhone4 apple:successorOf apple:IPhone4S" in s
    assert "apple:IPhone4 apple:predecessorOf apple:IPhone3GS" in s
    assert added == {"product": 1, "successor": 1, "predecessor": 1}


# ── Orchestration ─────────────────────────────────────────────────────────

def test_enrich_products_aggregates_stats_across_calls():
    products = [
        {"uri": "x1", "name": "P1",
         "predecessor": None, "predecessor_name": None,
         "successor": "x2",   "successor_name": "P2"},
        {"uri": "x3", "name": "P3",
         "predecessor": "x4", "predecessor_name": "P4",
         "successor": None,   "successor_name": None},
    ]
    counts = [
        {"product": 1, "successor": 1, "predecessor": 0},
        {"product": 1, "successor": 0, "predecessor": 1},
    ]
    with patch.object(dp, "fetch_apple_products", return_value=products), \
         patch.object(dp, "insert_product", side_effect=counts), \
         patch("backend.ingest.dbpedia_products.time.sleep"):
        stats = dp.enrich_products(sleep_between=0)
    assert stats == {
        "products_fetched":      2,
        "products_added":        2,
        "successor_added":       1,
        "predecessor_added":     1,
    }
