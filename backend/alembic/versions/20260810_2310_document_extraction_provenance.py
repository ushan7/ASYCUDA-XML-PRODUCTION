"""record where each document's extraction came from

Adds ``uploaded_document.extraction_provenance``
(domain.enums.ExtractionProvenance): OCR / BUNDLED_DEMO / CLIENT_FIXTURE.

Every existing row is OCR, and that is a statement of fact rather than a
convenient default: until this column existed, the only way a non-OCR
extraction could be stored was the fixture replay hook, which is refused
unless a deployment explicitly turns EASYCUSTOMS_ALLOW_FIXTURE_UPLOADS on.
A production database therefore holds nothing else.  Backfilling anything
narrower would be inventing provenance for documents nobody can re-examine,
which is the exact failure this column exists to prevent.

NOT NULL with a server default, so the backfill and the constraint land in
one step and rows written by an older process during a rolling deploy are
still valid.

Revision ID: b7c4e1a92f30
Revises: fa843d18c61e
Created: 2026-08-10 23:10:04.512877
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'b7c4e1a92f30'
down_revision = 'fa843d18c61e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "uploaded_document",
        sa.Column("extraction_provenance", sa.String(length=24),
                  nullable=False, server_default="OCR"),
    )


def downgrade() -> None:
    op.drop_column("uploaded_document", "extraction_provenance")
