"""Shared ingest steps used by every UE strategy.

``ensure_clean_document`` is idempotent for a given snapshot: if the snapshot
already exists, it returns the existing document; otherwise it fetches
Wikipedia, persists raw + clean, and returns the new document. UE1's chunker
and UE2's tree-builder both call this and add their own derived artefacts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text

from backend.config import get_settings
from backend.data import repo
from backend.data.cleaner import CleanSection, clean_html
from backend.data.pg import session_scope
from backend.data.wikipedia_loader import fetch_article

log = logging.getLogger(__name__)


@dataclass
class CleanResult:
    snapshot_id: int
    snapshot_created: bool
    document_id: int
    document_created: bool
    flat_sections: list[tuple[int, str, str]]  # (section_id, path, text)


def _walk_sections(
    session,
    sections: list[CleanSection],
    *,
    document_id: int,
    parent_id: int | None,
    order_counter: list[int],
    out_sections: list[tuple[int, str, str]],
) -> None:
    """Persist sections recursively in a single transaction.

    Reusing one session is mandatory — under READ COMMITTED a fresh session
    cannot see uncommitted parent INSERTs from another session, which would
    trip the parent_id foreign key. ``flush()`` after every insert makes the
    new row visible to the next INSERT within the same transaction.
    """
    for sec in sections:
        section_id = repo.insert_section(
            session,
            document_id=document_id,
            parent_id=parent_id,
            level=sec.level,
            heading=sec.heading,
            path=sec.path,
            order_idx=order_counter[0],
            text_=sec.text,
        )
        session.flush()
        order_counter[0] += 1
        if sec.text.strip():
            out_sections.append((section_id, sec.path, sec.text))
        _walk_sections(
            session,
            sec.children,
            document_id=document_id,
            parent_id=section_id,
            order_counter=order_counter,
            out_sections=out_sections,
        )


def ensure_clean_document(force: bool = False) -> CleanResult:
    """Fetch Wikipedia, persist raw snapshot + clean document/sections.

    Idempotent: if the snapshot hash already exists and ``force`` is False,
    it returns the existing clean document and skips the re-clean step.
    """
    settings = get_settings()

    log.info("Common ingest: fetching %s", settings.wikipedia_url)
    fetched = fetch_article(settings.wikipedia_url)

    # ── raw ──
    with session_scope() as session:
        snapshot_id, snapshot_created = repo.insert_or_get_snapshot(
            session,
            url=fetched.url,
            html=fetched.html,
            content_hash=fetched.content_hash,
            revision_id=fetched.revision_id,
            etag=fetched.etag,
        )

    # If snapshot is unchanged, check whether a clean document already exists.
    # Reuse it unless the caller forces a rebuild.
    if not snapshot_created and not force:
        with session_scope() as session:
            existing_doc = session.execute(
                text("SELECT id FROM clean.document WHERE snapshot_id = :s"),
                {"s": snapshot_id},
            ).scalar()
            if existing_doc:
                flat = _load_flat_sections(int(existing_doc))
                log.info("Common ingest: reusing existing clean document %d (%d sections)",
                         existing_doc, len(flat))
                return CleanResult(
                    snapshot_id=snapshot_id,
                    snapshot_created=False,
                    document_id=int(existing_doc),
                    document_created=False,
                    flat_sections=flat,
                )

    # ── clean ──
    log.info("Common ingest: cleaning HTML → Markdown + sections")
    clean = clean_html(fetched.html, fetched.title)
    with session_scope() as session:
        document_id = repo.upsert_clean_document(
            session,
            snapshot_id=snapshot_id,
            title=clean.title,
            markdown=clean.markdown,
        )

    flat_sections: list[tuple[int, str, str]] = []
    with session_scope() as session:
        _walk_sections(
            session,
            clean.sections,
            document_id=document_id,
            parent_id=None,
            order_counter=[0],
            out_sections=flat_sections,
        )
    log.info("Common ingest: persisted %d sections", len(flat_sections))

    return CleanResult(
        snapshot_id=snapshot_id,
        snapshot_created=snapshot_created,
        document_id=document_id,
        document_created=True,
        flat_sections=flat_sections,
    )


def _load_flat_sections(document_id: int) -> list[tuple[int, str, str]]:
    """Read clean.section rows for a document in their original order."""
    with session_scope() as session:
        rows = session.execute(
            text(
                "SELECT id, path, text FROM clean.section "
                "WHERE document_id = :d ORDER BY order_idx ASC"
            ),
            {"d": document_id},
        ).all()
    return [(int(r[0]), r[1], r[2]) for r in rows if (r[2] or "").strip()]
