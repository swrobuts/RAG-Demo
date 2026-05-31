"""Tests for the Wikipedia section-co-occurrence edge enrichment."""
from unittest.mock import MagicMock, patch

from backend.ingest import cooccurrence as co


def test_compute_cooccurrences_counts_pairs_per_section():
    # Two sections; entities iPhone & iOS co-occur in both;
    # Macintosh only in section A.
    sections = {
        "/Apple/Geschichte":   ["PRODUCT:ios", "PRODUCT:iphone", "PRODUCT:macintosh"],
        "/Apple/Produkte":     ["PRODUCT:ios", "PRODUCT:iphone"],
    }
    with patch.object(co, "fetch_section_mentions", return_value=sections):
        counts = co.compute_cooccurrences()
    # Pair keys are canonicalised so src < tgt — "PRODUCT:ios" sorts
    # before "PRODUCT:iphone" because 'o' < 'p' at index 9.
    assert counts[("PRODUCT:ios",    "PRODUCT:iphone")] == 2
    assert counts[("PRODUCT:iphone", "PRODUCT:macintosh")] == 1
    assert counts[("PRODUCT:ios",    "PRODUCT:macintosh")] == 1


def test_compute_cooccurrences_canonicalises_pair_order():
    """(b, a) should be stored as (a, b) when a < b — no double counting."""
    sections = {
        "S1": ["B", "A"],
        "S2": ["A", "B"],
    }
    with patch.object(co, "fetch_section_mentions", return_value=sections):
        counts = co.compute_cooccurrences()
    assert counts == {("A", "B"): 2}


def test_insert_cooccurrence_edges_respects_threshold():
    pair_counts = {("A", "B"): 3, ("A", "C"): 1, ("B", "C"): 2}
    fake_session = MagicMock()
    fake_session.run.return_value.single.return_value = {"r": "ok"}
    fake_ctx = MagicMock()
    fake_ctx.__enter__.return_value = fake_session
    with patch.object(co, "neo4j_session", return_value=fake_ctx):
        stats = co.insert_cooccurrence_edges(pair_counts, min_count=2)
    # Only A-B (3) and B-C (2) cross the threshold; A-C (1) is dropped
    assert stats["pairs_above_threshold"] == 2
    assert stats["pairs_below_threshold"] == 1
    assert stats["threshold"] == 2
    assert fake_session.run.call_count == 2


def test_insert_cooccurrence_edges_with_default_threshold():
    """Default MIN_COOCCUR = 2 — pair counts of exactly 2 should pass."""
    pair_counts = {("X", "Y"): 2}
    fake_session = MagicMock()
    fake_session.run.return_value.single.return_value = {"r": "ok"}
    fake_ctx = MagicMock()
    fake_ctx.__enter__.return_value = fake_session
    with patch.object(co, "neo4j_session", return_value=fake_ctx):
        stats = co.insert_cooccurrence_edges(pair_counts)
    assert stats["pairs_above_threshold"] == 1
