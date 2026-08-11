"""move the vendor layout / field-profile stores out of JSON files

Adds ``vendor_layout`` and ``vendor_field_profile``, replacing
``storage/vendor_layouts.json`` and ``storage/vendor_field_profiles.json``.

The files were not merely a slower place to keep this.  Both were whole-file
read-modify-write: load the document, edit it in memory, ``os.replace`` it back.
That is atomic for ONE writer and lossy for two — two processes recording a
layout in the same moment each started from the same snapshot, and the second
write silently discarded everything the first had learned.  A local file is also
invisible to a second container by construction.  Between them, those two
properties are a reason the application could not run more than one process.

A row per key makes concurrent writers independent, and what genuinely remains a
read-modify-write (the counters, and the COO source state machine) is done under
``SELECT ... FOR UPDATE``.

No backfill here.  The rows come from the legacy files at startup instead
(``app/database.py::import_legacy_side_stores``), because the files live in the
application's storage directory, which a migration running as a separate one-shot
container or a DBA session has no reason to be able to read.  That import is
idempotent by key, so it is safe to run on every boot and safe to run after this.

Portable column types only (String/JSON/Integer/DateTime), like the baseline:
the same revision has to build a SQLite file and a Postgres database.

Revision ID: c93a5d21e4b8
Revises: b7c4e1a92f30
Created: 2026-08-11 00:15:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'c93a5d21e4b8'
down_revision = 'b7c4e1a92f30'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vendor_layout",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("role", sa.String(length=40), nullable=False),
        # NOT NULL on purpose: layout_memory refuses to record a layout that did
        # not come from the document's own header, because such an entry matches
        # every headerless document of the role and then confirms itself.
        sa.Column("header_signature", sa.String(length=255), nullable=False),
        sa.Column("mapping", sa.JSON(), nullable=False),
        sa.Column("vendor_hint", sa.String(length=160), nullable=True),
        sa.Column("confirmed_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("docs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("role", "header_signature",
                            name="uq_vendor_layout_role_signature"),
    )
    op.create_index("ix_vendor_layout_role", "vendor_layout", ["role"])

    op.create_table(
        "vendor_field_profile",
        # The normalised vendor key (field_profiles._norm_vendor) is the identity.
        sa.Column("vendor", sa.String(length=160), primary_key=True),
        sa.Column("display", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("coo_default", sa.String(length=2), nullable=True),
        sa.Column("coo_source", sa.String(length=20), nullable=True),
        sa.Column("coo_docs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("vendor_field_profile")
    op.drop_index("ix_vendor_layout_role", table_name="vendor_layout")
    op.drop_table("vendor_layout")
