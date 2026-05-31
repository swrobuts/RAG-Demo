"""Tests for UE4 helpers — URI generation, property mapping, TTL validity."""
from pathlib import Path

from backend.ingest.ue4_ontology import (
    _build_relation_triple,
    _entity_uri,
    _map_property,
    _Relation,
    _safe_json,
    _sparql_literal,
)


def test_entity_uri_strips_diacritics():
    assert _entity_uri("PERSON:steve jobs") == "http://uc5.butscher.cloud/apple#SteveJobs"
    assert _entity_uri("PERSON:jérôme") == "http://uc5.butscher.cloud/apple#Jerome"
    assert _entity_uri("ORGANIZATION:apple") == "http://uc5.butscher.cloud/apple#Apple"


def test_entity_uri_falls_back_for_empty():
    assert _entity_uri("PERSON:") == "http://uc5.butscher.cloud/apple#Unknown"
    assert _entity_uri(":   ") == "http://uc5.butscher.cloud/apple#Unknown"


def test_map_property_known():
    assert _map_property("FOUNDED") == "apple:founded"
    assert _map_property("GRUENDET") == "apple:founded"
    assert _map_property("DESIGNED_BY") == "apple:designedBy"
    assert _map_property("HAS_CEO") == "apple:hasCEO"


def test_map_property_unknown_falls_back():
    assert _map_property("UNKNOWN_REL") == "apple:associatedWith"
    assert _map_property("") == "apple:associatedWith"
    assert _map_property(None) == "apple:associatedWith"  # type: ignore[arg-type]


def test_sparql_literal_escapes():
    assert _sparql_literal('Tim "Apple" Cook') == r'"Tim \"Apple\" Cook"'
    assert _sparql_literal("line\nbreak") == '"line break"'


def test_relation_triple_format():
    r = _Relation(src_key="PERSON:steve jobs", src_type="PERSON",
                  tgt_key="ORGANIZATION:apple", tgt_type="ORGANIZATION",
                  rel_type="FOUNDED", weight=3)
    t = _build_relation_triple(r)
    assert t == "<http://uc5.butscher.cloud/apple#SteveJobs> apple:founded <http://uc5.butscher.cloud/apple#Apple> ."


def test_safe_json_handles_codefence_and_prose():
    assert _safe_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _safe_json('hier ist mein urteil: {"x": 2} fertig.') == {"x": 2}
    assert _safe_json("kein JSON") is None
    assert _safe_json("") is None


def test_ontology_ttl_is_well_formed():
    """The shipped ontology must be valid Turtle so GraphDB accepts it."""
    from rdflib import Graph
    ttl = Path(__file__).resolve().parents[2] / "data" / "migrations" / "graphdb" / "001_apple_ontology.ttl"
    g = Graph()
    g.parse(str(ttl), format="turtle")
    # Sanity: should have hundreds of triples, not a stub.
    assert len(g) > 100, f"ontology too small ({len(g)} triples)"


def test_ontology_defines_ceo_subclass_of_executive():
    """The reasoner depends on this hierarchy — pin it down."""
    from rdflib import Graph, URIRef
    from rdflib.namespace import RDFS
    ttl = Path(__file__).resolve().parents[2] / "data" / "migrations" / "graphdb" / "001_apple_ontology.ttl"
    g = Graph()
    g.parse(str(ttl), format="turtle")
    ceo = URIRef("http://uc5.butscher.cloud/apple#CEO")
    executive = URIRef("http://uc5.butscher.cloud/apple#Executive")
    assert (ceo, RDFS.subClassOf, executive) in g


# ── _ensure_prefixes ────────────────────────────────────────────────────────

from backend.retrieval.ontology import _ensure_prefixes


def test_ensure_prefixes_adds_missing():
    q = "SELECT ?p WHERE { ?p apple:wasCEOOf apple:AppleInc }"
    out = _ensure_prefixes(q)
    assert "PREFIX apple: <http://uc5.butscher.cloud/apple#>" in out
    assert q in out  # body preserved


def test_ensure_prefixes_handles_multiple_namespaces():
    q = "SELECT ?n WHERE { ?p apple:foundedBy ?p2 . ?p2 foaf:name ?n }"
    out = _ensure_prefixes(q)
    assert "PREFIX apple:" in out
    assert "PREFIX foaf:" in out
    assert "PREFIX rdfs:" not in out  # not used in body


def test_ensure_prefixes_keeps_already_declared():
    q = "PREFIX apple: <http://uc5.butscher.cloud/apple#>\nSELECT ?p WHERE { ?p apple:foundedBy apple:AppleInc }"
    out = _ensure_prefixes(q)
    # Only one PREFIX apple: line (the original)
    assert out.count("PREFIX apple:") == 1
    # Body unchanged
    assert "SELECT ?p" in out


def test_ensure_prefixes_no_op_for_empty():
    assert _ensure_prefixes("") == ""
