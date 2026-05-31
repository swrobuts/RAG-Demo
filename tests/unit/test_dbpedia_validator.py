"""Tests for the DBpedia validator.

Network calls (to DBpedia) and graph mutations (to GraphDB) are mocked.
We test the parsing logic, the SPARQL strings actually generated, and
the orchestration sequencing — not the live endpoints."""
from unittest.mock import patch

from backend.ingest import dbpedia_validator as dv


# ── Utilities ─────────────────────────────────────────────────────────────

def test_slugify_strips_non_alnum():
    assert dv._slugify("Steve Jobs") == "SteveJobs"
    assert dv._slugify("Mike H. Spindler") == "MikeHSpindler"
    assert dv._slugify("  ") == ""


def test_escape_literal_quotes_and_backslashes_and_newlines():
    assert dv._escape_literal('hello "world"') == 'hello \\"world\\"'
    assert dv._escape_literal("line\nbreak") == "line break"
    assert dv._escape_literal("back\\slash") == "back\\\\slash"


# ── Step 1: fetch canonical persons ───────────────────────────────────────

def test_fetch_canonical_persons_parses_bindings():
    fake_response = {
        "results": {
            "bindings": [
                {"person": {"value": "http://dbpedia.org/resource/Steve_Jobs"},
                 "name":   {"value": "Steve Jobs"},
                 "role":   {"value": "Founder"}},
                {"person": {"value": "http://dbpedia.org/resource/Tim_Cook"},
                 "name":   {"value": "Tim Cook"},
                 "role":   {"value": "Executive"}},
            ]
        }
    }
    with patch.object(dv, "_dbpedia_query", return_value=fake_response):
        out = dv.fetch_canonical_persons()
    assert out == [
        {"person_uri": "http://dbpedia.org/resource/Steve_Jobs",
         "name": "Steve Jobs", "role": "Founder"},
        {"person_uri": "http://dbpedia.org/resource/Tim_Cook",
         "name": "Tim Cook", "role": "Executive"},
    ]


def test_fetch_canonical_persons_deduplicates_person_role_pairs():
    fake_response = {
        "results": {
            "bindings": [
                {"person": {"value": "http://dbpedia.org/resource/Steve_Jobs"},
                 "name":   {"value": "Steve Jobs"},
                 "role":   {"value": "Founder"}},
                # DBpedia sometimes returns the same pair twice with different
                # label variants when joining over multiple infoboxes.
                {"person": {"value": "http://dbpedia.org/resource/Steve_Jobs"},
                 "name":   {"value": "Steve Jobs"},
                 "role":   {"value": "Founder"}},
            ]
        }
    }
    with patch.object(dv, "_dbpedia_query", return_value=fake_response):
        out = dv.fetch_canonical_persons()
    assert len(out) == 1


def test_fetch_canonical_persons_returns_empty_on_failure():
    with patch.object(dv, "_dbpedia_query", return_value={}):
        assert dv.fetch_canonical_persons() == []


# ── Step 1b: insert canonical person ──────────────────────────────────────

def test_insert_canonical_person_skips_when_already_present():
    """If GraphDB already has the (sameAs, role) combination, do nothing."""
    with patch.object(dv, "_person_already_present", return_value=True), \
         patch.object(dv.graphdb_client, "update") as mock_update:
        assert dv.insert_canonical_person({
            "person_uri": "http://dbpedia.org/resource/Steve_Jobs",
            "name": "Steve Jobs",
            "role": "Founder",
        }) is False
        mock_update.assert_not_called()


def test_insert_canonical_person_inserts_with_correct_uri():
    captured = {}
    def _capture(sparql):
        captured["sparql"] = sparql
    with patch.object(dv, "_person_already_present", return_value=False), \
         patch.object(dv.graphdb_client, "update", side_effect=_capture):
        assert dv.insert_canonical_person({
            "person_uri": "http://dbpedia.org/resource/Tim_Cook",
            "name": "Tim Cook",
            "role": "Executive",
        }) is True
    s = captured["sparql"]
    # The apple:URI uses the slugified name, NOT the DBpedia local name
    assert "apple:TimCook" in s
    assert "a apple:Executive" in s
    assert "a apple:Person" in s
    assert "<http://dbpedia.org/resource/Tim_Cook>" in s
    assert "owl:sameAs" in s
    assert 'rdfs:label "Tim Cook"@en' in s


def test_insert_canonical_person_rejects_unknown_role():
    with patch.object(dv.graphdb_client, "update") as mock_update:
        assert dv.insert_canonical_person({
            "person_uri": "x", "name": "X", "role": "Mascot",
        }) is False
        mock_update.assert_not_called()


# ── Step 2: verification ──────────────────────────────────────────────────

def test_is_apple_related_true():
    with patch.object(dv, "_dbpedia_query", return_value={"boolean": True}):
        assert dv.is_apple_related("Tim Cook") is True


def test_is_apple_related_false_on_negative_ask():
    with patch.object(dv, "_dbpedia_query", return_value={"boolean": False}):
        assert dv.is_apple_related("Aljaksandr Lukaschenka") is False


def test_is_apple_related_false_on_dbpedia_outage():
    with patch.object(dv, "_dbpedia_query", return_value={}):
        # DBpedia down → ASK returns no `boolean` key → falsy.
        # We deliberately treat this as "unknown → leave alone" higher up,
        # but the helper itself returns False.
        assert dv.is_apple_related("Tim Cook") is False


def test_is_apple_related_escapes_quotes_in_name():
    captured = {}
    def _capture(sparql, **kw):
        captured["sparql"] = sparql
        return {"boolean": False}
    with patch.object(dv, "_dbpedia_query", side_effect=_capture):
        dv.is_apple_related('quote " inside')
    assert 'quote \\" inside' in captured["sparql"]


def test_demote_to_unrelated_inserts_correct_triple():
    captured = {}
    def _capture(sparql):
        captured["sparql"] = sparql
    with patch.object(dv.graphdb_client, "update", side_effect=_capture):
        dv.demote_to_unrelated("http://uc5.butscher.cloud/apple#Foo")
    assert "apple:UnrelatedPerson" in captured["sparql"]
    assert "<http://uc5.butscher.cloud/apple#Foo>" in captured["sparql"]


# ── Orchestration ─────────────────────────────────────────────────────────

def test_validate_and_enrich_full_flow():
    canonical = [
        {"person_uri": "http://dbpedia.org/resource/Tim_Cook",
         "name": "Tim Cook", "role": "Executive"},
    ]
    unverified = [
        {"uri": "http://uc5.butscher.cloud/apple#Alan_Turing", "name": "Alan Turing"},
        {"uri": "http://uc5.butscher.cloud/apple#Tim_Cook",    "name": "Tim Cook"},
    ]
    # Tim Cook → confirmed; Turing → demoted.
    def fake_is_apple_related(name):
        return name == "Tim Cook"

    with patch.object(dv, "fetch_canonical_persons", return_value=canonical), \
         patch.object(dv, "insert_canonical_person", return_value=True) as mock_ins, \
         patch.object(dv, "list_unverified_persons", return_value=unverified), \
         patch.object(dv, "is_apple_related", side_effect=fake_is_apple_related), \
         patch.object(dv, "demote_to_unrelated") as mock_demote, \
         patch("backend.ingest.dbpedia_validator.time.sleep"):
        stats = dv.validate_and_enrich(sleep_between_queries=0)

    assert stats == {
        "canonical_persons_fetched": 1,
        "canonical_persons_added":   1,
        "unverified_persons_total":  2,
        "persons_confirmed":         1,
        "persons_demoted":           1,
    }
    mock_ins.assert_called_once()
    mock_demote.assert_called_once_with("http://uc5.butscher.cloud/apple#Alan_Turing")
