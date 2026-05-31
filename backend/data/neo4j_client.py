"""Thin wrapper around the official Neo4j driver.

Connection is lazy: ``get_driver()`` creates a singleton on first use. Sessions
are managed via a context manager so callers don't have to remember to close.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from neo4j import Driver, GraphDatabase, Session

from backend.config import get_settings

log = logging.getLogger(__name__)

_driver: Driver | None = None

# Migration files live alongside the Postgres ones.
NEO4J_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "data" / "migrations" / "neo4j"


def get_driver() -> Driver:
    global _driver
    if _driver is None:
        settings = get_settings()
        _driver = GraphDatabase.driver(
            settings.neo4j_url,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
    return _driver


def close_driver() -> None:
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


@contextmanager
def neo4j_session() -> Iterator[Session]:
    driver = get_driver()
    session = driver.session()
    try:
        yield session
    finally:
        session.close()


def ping() -> bool:
    try:
        with neo4j_session() as session:
            session.run("RETURN 1").consume()
        return True
    except Exception:  # noqa: BLE001
        return False


def _split_cypher_statements(script: str) -> list[str]:
    """Split a multi-statement Cypher script on semicolons that end statements.
    Naïve but enough for our migrations (no string-literal semicolons)."""
    parts: list[str] = []
    for chunk in script.split(";"):
        s = chunk.strip()
        if s:
            parts.append(s)
    return parts


def apply_neo4j_migrations() -> list[str]:
    """Run every .cypher file under data/migrations/neo4j once. Tracks applied
    versions in a (:Migration {version}) node so re-runs are idempotent."""
    applied: list[str] = []
    files = sorted(NEO4J_MIGRATIONS_DIR.glob("*.cypher"))
    if not files:
        return applied
    with neo4j_session() as session:
        session.run("CREATE CONSTRAINT migration_version IF NOT EXISTS "
                    "FOR (m:Migration) REQUIRE m.version IS UNIQUE").consume()
        existing = {
            r["v"] for r in session.run("MATCH (m:Migration) RETURN m.version AS v")
        }
        for path in files:
            version = path.stem
            if version in existing:
                continue
            log.info("Applying Neo4j migration %s", version)
            script = path.read_text(encoding="utf-8")
            for stmt in _split_cypher_statements(script):
                session.run(stmt).consume()
            session.run("MERGE (m:Migration {version: $v})", v=version).consume()
            applied.append(version)
    return applied


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    out = apply_neo4j_migrations()
    print("Applied:", out if out else "(none)")
