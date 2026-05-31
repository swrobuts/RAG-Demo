"""GraphDB REST client for UE4 (Ontology-RAG).

Uses the standard SPARQL 1.1 endpoints exposed by GraphDB at
``/repositories/{repo}`` (read) and ``/repositories/{repo}/statements``
(write). Also wraps the repository management endpoints so the ingest
pipeline can create the repo on first run if it doesn't exist yet.

GraphDB Community can run without auth (default) or with RBAC enabled —
both modes are supported via the optional GRAPHDB_USER/PASSWORD env vars.
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from backend.config import get_settings

log = logging.getLogger(__name__)

# Where TTL files live in the repo (alongside Postgres/Neo4j migrations).
GRAPHDB_ASSETS_DIR = Path(__file__).resolve().parents[2] / "data" / "migrations" / "graphdb"


def _auth() -> tuple[str, str] | None:
    s = get_settings()
    if s.graphdb_user and s.graphdb_password:
        return (s.graphdb_user, s.graphdb_password)
    return None


def _base_url() -> str:
    return get_settings().graphdb_url.rstrip("/")


def _repo_url() -> str:
    s = get_settings()
    return f"{_base_url()}/repositories/{s.graphdb_repo}"


def _statements_url() -> str:
    return f"{_repo_url()}/statements"


# ── Health ─────────────────────────────────────────────────────────────────

def ping() -> bool:
    """True if the configured repository answers a trivial SPARQL ASK."""
    try:
        with httpx.Client(timeout=5.0, auth=_auth()) as c:
            r = c.get(
                _repo_url(),
                params={"query": "ASK { ?s ?p ?o }"},
                headers={"Accept": "application/sparql-results+json"},
            )
            return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def list_repositories() -> list[str]:
    """Return repository IDs known to the GraphDB instance."""
    with httpx.Client(timeout=10.0, auth=_auth()) as c:
        r = c.get(f"{_base_url()}/rest/repositories",
                  headers={"Accept": "application/json"})
    r.raise_for_status()
    return [item["id"] for item in r.json()]


def repository_exists() -> bool:
    try:
        return get_settings().graphdb_repo in list_repositories()
    except Exception:  # noqa: BLE001
        return False


def ensure_repository(reasoning: str = "owl-horst-optimized") -> str:
    """Create the configured repository if absent. Returns a status string.

    ``reasoning`` picks the ruleset; ``owl-horst-optimized`` is a good
    balance (subclass + subproperty + inverse + transitive properties)
    without the full owl-max overhead. Other options: ``rdfsplus-optimized``,
    ``owl-max-optimized``, ``empty`` (no reasoning).

    GraphDB 11's REST API expects the TTL config either as a multipart form
    field (``config=@file.ttl``) OR via PUT to ``/rest/repositories/{id}``
    with text/turtle. We use the multipart variant — well-supported across
    versions."""
    s = get_settings()
    if repository_exists():
        return f"repo {s.graphdb_repo!r} already exists"
    config_ttl = _build_repo_config(s.graphdb_repo, reasoning)
    files = {"config": ("config.ttl", config_ttl.encode("utf-8"), "text/turtle")}
    with httpx.Client(timeout=30.0, auth=_auth()) as c:
        r = c.post(f"{_base_url()}/rest/repositories", files=files)
        if r.status_code == 400 and "already exist" in r.text.lower():
            return f"repo {s.graphdb_repo!r} already exists (race)"
        if r.status_code >= 400:
            # Surface GraphDB's own error message — much easier to diagnose
            # than a generic httpx HTTPStatusError.
            raise RuntimeError(
                f"GraphDB rejected repo creation (HTTP {r.status_code}): {r.text[:400]}"
            )
    return f"created repo {s.graphdb_repo!r} with reasoning={reasoning}"


def _build_repo_config(repo_id: str, ruleset: str) -> str:
    """SAIL config TTL for GraphDB 11.x. The namespace was renamed from
    ``owlim:`` to ``graphdb:`` somewhere around v10, and the property set
    was trimmed — modern config only needs the handful of keys below."""
    return f"""@prefix rdfs:    <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rep:     <http://www.openrdf.org/config/repository#> .
@prefix sr:      <http://www.openrdf.org/config/repository/sail#> .
@prefix sail:    <http://www.openrdf.org/config/sail#> .
@prefix graphdb: <http://www.ontotext.com/config/graphdb#> .

[] a rep:Repository ;
    rep:repositoryID "{repo_id}" ;
    rdfs:label "UC5 RAG Apple — Ontology-RAG repository" ;
    rep:repositoryImpl [
        rep:repositoryType "graphdb:SailRepository" ;
        sr:sailImpl [
            sail:sailType "graphdb:Sail" ;
            graphdb:base-URL "http://uc5.butscher.cloud/apple#" ;
            graphdb:defaultNS "" ;
            graphdb:entity-index-size "10000000" ;
            graphdb:enable-context-index "false" ;
            graphdb:imports "" ;
            graphdb:repository-type "file-repository" ;
            graphdb:ruleset "{ruleset}" ;
            graphdb:disable-sameAs "false" ;
            graphdb:storage-folder "storage" ;
            graphdb:query-timeout "30" ;
            graphdb:query-limit-results "10000" ;
            graphdb:throw-QueryEvaluationException-on-timeout "false" ;
            graphdb:read-only "false"
        ]
    ] .
"""


# ── SPARQL ─────────────────────────────────────────────────────────────────

def select(sparql_query: str) -> dict:
    """Run a SPARQL SELECT/ASK/DESCRIBE/CONSTRUCT; return parsed JSON."""
    with httpx.Client(timeout=30.0, auth=_auth()) as c:
        r = c.post(
            _repo_url(),
            data={"query": sparql_query},
            headers={"Accept": "application/sparql-results+json"},
        )
    r.raise_for_status()
    return r.json()


def update(sparql_update: str) -> None:
    """Run a SPARQL 1.1 UPDATE (INSERT/DELETE/CLEAR/LOAD)."""
    with httpx.Client(timeout=60.0, auth=_auth()) as c:
        r = c.post(
            _statements_url(),
            data={"update": sparql_update},
            headers={"Accept": "application/json"},
        )
    r.raise_for_status()


def clear_all() -> None:
    """Wipe all triples in the repo (the ontology TTL must be re-uploaded)."""
    update("CLEAR ALL")


def upload_ttl(ttl_text: str, *, named_graph: str | None = None) -> None:
    """POST raw Turtle into the repository. Optional named-graph URI."""
    params = {}
    if named_graph:
        params["context"] = f"<{named_graph}>"
    with httpx.Client(timeout=60.0, auth=_auth()) as c:
        r = c.post(
            _statements_url(),
            params=params,
            content=ttl_text.encode("utf-8"),
            headers={"Content-Type": "text/turtle"},
        )
    r.raise_for_status()


def upload_ontology_file(path: Path) -> None:
    """Convenience: upload a .ttl file by path."""
    upload_ttl(path.read_text(encoding="utf-8"))


def count_triples() -> int:
    res = select("SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }")
    try:
        return int(res["results"]["bindings"][0]["n"]["value"])
    except (KeyError, IndexError, ValueError):
        return 0


# ── Migrations ─────────────────────────────────────────────────────────────

def apply_graphdb_setup() -> dict:
    """Create the repo if needed and upload every .ttl under
    data/migrations/graphdb/. Idempotent — re-runs simply re-upload (GraphDB
    deduplicates triples).

    Errors uploading individual TTL files are logged loudly and recorded
    in the returned ``failed`` list, but do not abort the whole run —
    that way one broken file doesn't take down UE4 entirely. (We learned
    this the hard way: ``apple:Eight_Inc.`` had a trailing dot, GraphDB
    silently rejected the whole file with HTTP 400, and the missing
    canonical-persons data only showed up days later as „Tim Cook fehlt".)
    """
    info: dict = {}
    info["repo_status"] = ensure_repository()
    files = sorted(GRAPHDB_ASSETS_DIR.glob("*.ttl"))
    uploaded: list[str] = []
    failed: list[dict] = []
    for f in files:
        try:
            upload_ontology_file(f)
            uploaded.append(f.name)
            log.info("Uploaded GraphDB ontology %s", f.name)
        except Exception as exc:  # noqa: BLE001
            # Show file name + a snippet of GraphDB's actual error so the
            # operator can diagnose without reading container logs raw.
            msg = str(exc)
            log.error("GraphDB UPLOAD FAILED: %s — %s",
                      f.name, msg[:500].replace("\n", " "))
            failed.append({"file": f.name, "error": msg[:500]})
    info["uploaded"] = uploaded
    info["failed"] = failed
    info["triple_count"] = count_triples()
    if failed:
        log.error("GraphDB setup: %d of %d files failed to upload",
                  len(failed), len(files))
    return info


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    print(apply_graphdb_setup())
