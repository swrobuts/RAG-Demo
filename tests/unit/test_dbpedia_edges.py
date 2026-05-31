"""Tests for the DBpedia cross-edges enrichment."""
from unittest.mock import MagicMock, patch

from backend.ingest import dbpedia_edges as de


def test_predicate_map_covers_common_relations():
    # spot-check that the most important predicates resolve
    assert de._PREDICATE_MAP["http://dbpedia.org/ontology/employer"] == "works_for"
    assert de._PREDICATE_MAP["http://dbpedia.org/property/founders"] == "founded_by"
    assert de._PREDICATE_MAP["http://dbpedia.org/ontology/designer"] == "designed_by"
    assert de._PREDICATE_MAP["http://dbpedia.org/ontology/predecessor"] == "predecessor_of"
    assert de._PREDICATE_MAP["http://dbpedia.org/ontology/manufacturer"] == "manufactured_by"


def test_fetch_cross_edges_filters_to_known_pairs_and_maps_predicates():
    dbr_to_key = {
        "http://dbpedia.org/resource/Apple_Inc.": "ORGANIZATION:apple",
        "http://dbpedia.org/resource/Steve_Jobs": "PERSON:steve jobs",
        "http://dbpedia.org/resource/Foxconn":    "ORGANIZATION:foxconn",
    }
    # DBpedia returns 3 triples:
    #  - Jobs founder Apple → known, map dbo:founder → founded_by
    #  - Apple keyPerson Cook → Cook NOT in our set, must drop
    #  - Apple manufacturer Foxconn → known, map dbo:manufacturer → manufactured_by
    fake = {"results": {"bindings": [
        {"s": {"value": "http://dbpedia.org/resource/Steve_Jobs"},
         "p": {"value": "http://dbpedia.org/ontology/founder"},
         "o": {"value": "http://dbpedia.org/resource/Apple_Inc."}},
        {"s": {"value": "http://dbpedia.org/resource/Apple_Inc."},
         "p": {"value": "http://dbpedia.org/ontology/keyPerson"},
         "o": {"value": "http://dbpedia.org/resource/Tim_Cook"}},
        {"s": {"value": "http://dbpedia.org/resource/Apple_Inc."},
         "p": {"value": "http://dbpedia.org/ontology/manufacturer"},
         "o": {"value": "http://dbpedia.org/resource/Foxconn"}},
    ]}}
    with patch.object(de, "_fetch_entities_with_dbpedia", return_value=dbr_to_key), \
         patch.object(de, "_dbpedia_query", return_value=fake):
        edges = de.fetch_cross_edges()
    # 2 kept, 1 dropped (Tim_Cook not in set)
    assert len(edges) == 2
    rels = sorted((e["src"], e["tgt"], e["rel"]) for e in edges)
    assert rels == [
        ("ORGANIZATION:apple", "ORGANIZATION:foxconn", "manufactured_by"),
        ("PERSON:steve jobs",  "ORGANIZATION:apple",   "founded_by"),
    ]


def test_fetch_cross_edges_drops_self_loops():
    fake = {"results": {"bindings": [
        {"s": {"value": "http://dbpedia.org/resource/X"},
         "p": {"value": "http://dbpedia.org/ontology/related"},
         "o": {"value": "http://dbpedia.org/resource/X"}},
    ]}}
    with patch.object(de, "_fetch_entities_with_dbpedia",
                      return_value={"http://dbpedia.org/resource/X": "TEST:x"}), \
         patch.object(de, "_dbpedia_query", return_value=fake):
        assert de.fetch_cross_edges() == []


def test_fetch_cross_edges_deduplicates():
    """Same (src, tgt, rel) triple appearing twice → one edge only."""
    fake = {"results": {"bindings": [
        {"s": {"value": "http://dbpedia.org/resource/A"},
         "p": {"value": "http://dbpedia.org/ontology/founder"},
         "o": {"value": "http://dbpedia.org/resource/B"}},
        # same again with the property: form — should dedupe
        {"s": {"value": "http://dbpedia.org/resource/A"},
         "p": {"value": "http://dbpedia.org/property/founder"},
         "o": {"value": "http://dbpedia.org/resource/B"}},
    ]}}
    with patch.object(de, "_fetch_entities_with_dbpedia", return_value={
            "http://dbpedia.org/resource/A": "X:a",
            "http://dbpedia.org/resource/B": "X:b"}), \
         patch.object(de, "_dbpedia_query", return_value=fake):
        edges = de.fetch_cross_edges()
    assert len(edges) == 1
    assert edges[0]["rel"] == "founded_by"


def test_fetch_cross_edges_unknown_predicate_falls_back():
    fake = {"results": {"bindings": [
        {"s": {"value": "http://dbpedia.org/resource/A"},
         "p": {"value": "http://dbpedia.org/ontology/exoticUnusedProperty"},
         "o": {"value": "http://dbpedia.org/resource/B"}},
    ]}}
    with patch.object(de, "_fetch_entities_with_dbpedia", return_value={
            "http://dbpedia.org/resource/A": "X:a",
            "http://dbpedia.org/resource/B": "X:b"}), \
         patch.object(de, "_dbpedia_query", return_value=fake):
        edges = de.fetch_cross_edges()
    assert edges[0]["rel"] == "associated_with"


def test_insert_cross_edges_uses_merge_per_edge():
    edges = [
        {"src": "X:a", "tgt": "X:b", "rel": "founded_by", "predicate": "p"},
        {"src": "X:a", "tgt": "X:c", "rel": "designed_by", "predicate": "p"},
    ]
    fake_session = MagicMock()
    fake_session.run.return_value.single.return_value = {"r": "ok"}
    fake_ctx = MagicMock()
    fake_ctx.__enter__.return_value = fake_session
    with patch.object(de, "neo4j_session", return_value=fake_ctx):
        n = de.insert_cross_edges(edges)
    assert n == 2
    assert fake_session.run.call_count == 2
