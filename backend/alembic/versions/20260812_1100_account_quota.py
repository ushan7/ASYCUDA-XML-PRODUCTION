"""give an account its own monthly document cap

Adds ``account_quota``.

The cap was one deployment-wide number, which is the right shape for exactly one
account and the wrong shape for two.  With more than one account it means a new
account's limit is "the same as everybody else's", recorded nowhere, and that
raising ONE account's limit raises everyone's — while the message
``metering.quota_exceeded`` already shows the reviewer promises the opposite
("or when an administrator raises the limit").  This table is what makes that
sentence true.

RESOLUTION ORDER, implemented in ``metering.resolved_cap``: the account's own
row if it has one with a number on it, otherwise the deployment default, and
``<= 0`` is unlimited at either level.

NO BACKFILL, and that is the design rather than an omission.  Absence means
"follow the deployment default", so a row per existing account would freeze
today's default onto every one of them and the default could never be moved
again.  Rows appear only where somebody sets a specific cap.  Nothing needs
backfilling on the counting side either: ``documents_this_month`` counts
``usage_event`` rows, which already exist for every account that has spent
anything.

``monthly_document_cap`` is NULLABLE on purpose — NULL is "follow the
deployment default" and 0 is "unlimited", which are different answers.  A row
added only to carry a ``note`` must not silently become a cap of zero.

``updated_by`` is nullable for a narrower reason: the admin route that sets a
cap and names its setter does not exist yet, so every row written before it does
was inserted by hand and has no setter to record.  NULL says that; an empty
string would read as a setter whose name was lost.

Revision ID: a3e9c2f71b48
Revises: f281d6b04a37
Created: 2026-08-12 11:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'a3e9c2f71b48'
down_revision = 'f281d6b04a37'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_quota",
        # The same key customs_job.owner_key holds.  Primary key outright: an
        # account has one allowance, not a history of them.
        sa.Column("owner_key", sa.String(length=160), primary_key=True),
        # NULL = follow the deployment default; 0 = unlimited.
        sa.Column("monthly_document_cap", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=120), nullable=True),
    )
    # No index beyond the primary key: every read is a single primary-key lookup
    # by owner_key, on the one line before an extraction spends money.


def downgrade() -> None:
    op.drop_table("account_quota")
