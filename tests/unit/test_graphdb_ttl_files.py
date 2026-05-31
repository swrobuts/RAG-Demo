"""Every .ttl file under data/migrations/graphdb/ must be syntactically
valid Turtle. We had a Turtle outage in production because
``apple:Eight_Inc.`` (trailing dot in PN_LOCAL) made GraphDB silently
reject the whole file — the failure was swallowed by the startup CMD's
``|| echo`` fallback, so we only noticed because Tim Cook wasn't in
the graph. This test catches that class of bug before it ships."""
from pathlib import Path

import rdflib

GRAPHDB_DIR = Path(__file__).resolve().parents[2] / "data" / "migrations" / "graphdb"


def test_all_ttl_files_parse():
    files = sorted(GRAPHDB_DIR.glob("*.ttl"))
    assert files, "no TTL files found — wrong path?"
    for f in files:
        g = rdflib.Graph()
        try:
            g.parse(f, format="turtle")
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"{f.name} failed to parse: {exc}") from exc
        assert len(g) > 0, f"{f.name} parsed empty — likely silent failure"
