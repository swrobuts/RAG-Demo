"""Plain-SQL migration runner.

Applies numbered ``.sql`` files from ``data/migrations/postgres/`` in order and
records each applied version in ``meta.schema_migration``. The first file must
create that table itself (chicken-and-egg) — the runner tolerates this by
opening a transaction per file and recording the version after the file runs.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from sqlalchemy import text

from backend.data.pg import get_engine

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "data" / "migrations" / "postgres"
VERSION_RE = re.compile(r"^(\d+)_.+\.sql$")


def _discover() -> list[Path]:
    files = [p for p in MIGRATIONS_DIR.glob("*.sql") if VERSION_RE.match(p.name)]
    return sorted(files, key=lambda p: int(VERSION_RE.match(p.name).group(1)))


def _applied_versions(conn) -> set[str]:
    exists = conn.execute(
        text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.tables"
            "  WHERE table_schema='meta' AND table_name='schema_migration'"
            ")"
        )
    ).scalar()
    if not exists:
        return set()
    rows = conn.execute(text("SELECT version FROM meta.schema_migration")).all()
    return {r[0] for r in rows}


def apply_migrations() -> list[str]:
    """Apply pending migrations. Returns the list of versions newly applied."""
    engine = get_engine()
    newly_applied: list[str] = []
    with engine.begin() as conn:
        applied = _applied_versions(conn)
        for path in _discover():
            version = path.stem  # e.g. "001_extensions_and_meta"
            if version in applied:
                continue
            log.info("Applying migration %s", version)
            sql = path.read_text(encoding="utf-8")
            conn.execute(text(sql))
            conn.execute(
                text("INSERT INTO meta.schema_migration (version) VALUES (:v) "
                     "ON CONFLICT (version) DO NOTHING"),
                {"v": version},
            )
            newly_applied.append(version)
    return newly_applied


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    applied = apply_migrations()
    if applied:
        print(f"Applied: {applied}")
    else:
        print("No new migrations.")
