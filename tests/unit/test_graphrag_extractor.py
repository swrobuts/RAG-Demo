"""Pure tests of the entity-extraction parser and entity-resolution helpers.
Runs without Neo4j or LLM."""
from backend.ingest.ue3_graphrag import (
    entity_key,
    normalize_entity_name,
    parse_extraction,
)


def test_normalize_strips_diacritics_and_punct():
    assert normalize_entity_name("Steve Jobs") == "steve jobs"
    assert normalize_entity_name("Apple Inc.") == "apple inc"
    assert normalize_entity_name("Sculley-Ära") == "sculley-ara"
    assert normalize_entity_name("  Macintosh  ") == "macintosh"


def test_entity_key_format():
    assert entity_key("Steve Jobs", "PERSON") == "PERSON:steve jobs"
    assert entity_key("Apple Inc.", "ORGANIZATION") == "ORGANIZATION:apple inc"


def test_parse_extraction_handles_clean_json():
    raw = """{
      "entities": [
        {"name": "Steve Jobs", "type": "PERSON", "description": "Mitgründer von Apple"},
        {"name": "Apple Inc.", "type": "ORGANIZATION", "description": "US-Unternehmen"}
      ],
      "relations": [
        {"source": "Steve Jobs", "target": "Apple Inc.", "type": "FOUNDED", "evidence": "..."}
      ]
    }"""
    ext = parse_extraction(raw, chunk_id=1)
    assert ext.chunk_id == 1
    assert len(ext.entities) == 2
    names = [e.name for e in ext.entities]
    assert "Steve Jobs" in names and "Apple Inc." in names
    assert len(ext.relations) == 1
    assert ext.relations[0].type == "FOUNDED"


def test_parse_extraction_strips_code_fence_and_prose():
    raw = """Hier das Ergebnis:
    ```json
    {"entities": [{"name": "iPhone", "type": "PRODUCT", "description": "Smartphone"}], "relations": []}
    ```"""
    ext = parse_extraction(raw, chunk_id=2)
    assert len(ext.entities) == 1
    assert ext.entities[0].type == "PRODUCT"


def test_parse_extraction_drops_invalid_types():
    raw = '{"entities": [{"name": "X", "type": "VEHICLE", "description": "y"}], "relations": []}'
    assert parse_extraction(raw, 1).entities == []


def test_parse_extraction_drops_dangling_relations():
    # Relation references an entity that wasn't extracted in the same chunk
    raw = """{
      "entities": [{"name": "Apple", "type": "ORGANIZATION", "description": "d"}],
      "relations": [{"source": "Apple", "target": "Microsoft", "type": "RIVAL", "evidence": "x"}]
    }"""
    ext = parse_extraction(raw, 1)
    assert ext.relations == []  # Microsoft not in entities → relation dropped


def test_parse_extraction_drops_self_relations():
    raw = """{
      "entities": [{"name": "Apple", "type": "ORGANIZATION", "description": "d"}],
      "relations": [{"source": "Apple", "target": "Apple", "type": "IS", "evidence": "x"}]
    }"""
    assert parse_extraction(raw, 1).relations == []


def test_parse_extraction_handles_garbage_response():
    assert parse_extraction("not JSON at all", 1).entities == []
    assert parse_extraction("", 1).entities == []


# ── Person-alias resolution ────────────────────────────────────────────────

from collections import Counter

from backend.ingest.ue3_graphrag import _resolve_person_aliases


def test_resolve_person_aliases_merges_lastname_into_fullname():
    entities = {
        "PERSON:steve wozniak": {"name": "Steve Wozniak", "type": "PERSON",
                                   "description": "Mitgründer von Apple"},
        "PERSON:wozniak": {"name": "Wozniak", "type": "PERSON",
                             "description": "kurz erwähnt"},
        "PERSON:tim cook": {"name": "Tim Cook", "type": "PERSON",
                              "description": "CEO ab 2011"},
        "PERSON:cook": {"name": "Cook", "type": "PERSON",
                          "description": "Apple-Chef"},
    }
    mentions = Counter({"PERSON:steve wozniak": 3, "PERSON:wozniak": 5,
                         "PERSON:tim cook": 2, "PERSON:cook": 4})

    aliases = _resolve_person_aliases(entities, mentions)

    assert aliases == {"PERSON:wozniak": "PERSON:steve wozniak",
                       "PERSON:cook": "PERSON:tim cook"}
    # alias rows removed
    assert "PERSON:wozniak" not in entities
    assert "PERSON:cook" not in entities
    # mention counts summed onto canonical
    assert mentions["PERSON:steve wozniak"] == 8
    assert mentions["PERSON:tim cook"] == 6
    # alias counts zeroed
    assert mentions["PERSON:wozniak"] == 0
    assert mentions["PERSON:cook"] == 0


def test_resolve_person_aliases_keeps_richer_description():
    entities = {
        "PERSON:steve wozniak": {"name": "Steve Wozniak", "type": "PERSON",
                                   "description": "kurz"},
        "PERSON:wozniak": {"name": "Wozniak", "type": "PERSON",
                             "description": "ein deutlich längerer Beschreibungssatz"},
    }
    mentions = Counter({"PERSON:steve wozniak": 1, "PERSON:wozniak": 1})
    _resolve_person_aliases(entities, mentions)
    # The canonical row absorbs the longer description even though it came
    # from the alias.
    assert "deutlich längerer" in entities["PERSON:steve wozniak"]["description"]


def test_resolve_person_aliases_ignores_non_person_types():
    entities = {
        "ORGANIZATION:apple inc": {"name": "Apple Inc", "type": "ORGANIZATION", "description": "x"},
        "ORGANIZATION:apple": {"name": "Apple", "type": "ORGANIZATION", "description": "y"},
    }
    mentions = Counter({"ORGANIZATION:apple inc": 1, "ORGANIZATION:apple": 1})
    aliases = _resolve_person_aliases(entities, mentions)
    # Same heuristic should NOT merge orgs (different mechanism would be needed)
    assert aliases == {}


def test_resolve_person_aliases_doesnt_merge_unrelated_persons():
    # Two persons whose names don't share a word stay separate.
    entities = {
        "PERSON:steve jobs": {"name": "Steve Jobs", "type": "PERSON", "description": "..."},
        "PERSON:bill gates": {"name": "Bill Gates", "type": "PERSON", "description": "..."},
    }
    mentions = Counter({"PERSON:steve jobs": 5, "PERSON:bill gates": 2})
    aliases = _resolve_person_aliases(entities, mentions)
    assert aliases == {}
    assert mentions["PERSON:steve jobs"] == 5
    assert mentions["PERSON:bill gates"] == 2
