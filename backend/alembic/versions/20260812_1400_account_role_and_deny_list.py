"""give the platform an administrator, and a way to bar an account

Adds ``account_role`` and ``account_disabled``.

WHY BOTH ARE LOCAL TABLES rather than Supabase ``app_metadata`` / a Supabase
user-disable: either would require the service-role key, which
``app/auth_supabase.py`` keeps deliberately out of this process because it would
turn any code-execution bug into control of the whole auth database.  Only the
anon key lives here, and nothing in these two tables needs more.

``account_role``: a row means ``admin``; ABSENCE MEANS MEMBER.  No row is written
for an ordinary user, so there is no signup-time write, no default to backfill,
and no migration to run when the meaning of "member" is "nothing recorded".  The
role is read server-side on the admin routes only and is never accepted from a
request body, a header or a token claim — a stateless 24h token cannot be
revoked, and late revocation is the direction that matters for a privilege.

What the role is FOR is settled by ``docs/ADR-001-identity-and-tenancy.md``: an
admin manages accounts, quotas and usage metadata and cannot read declarations.
``services.job_visible_to`` does not know this table exists, and that is the
design constraint rather than an outcome.

``account_disabled``: a row means this account may not sign in.  Also a list
rather than a flag column, for the same reason ``account_quota`` is a table of
exceptions — absence is the normal state and the common case writes nothing.

NO BACKFILL for either, and no ``role`` column added to any existing table: an
account with no row is a member in good standing, which is every account that
exists today.

Revision ID: c7b41e0d5a92
Revises: a3e9c2f71b48
Created: 2026-08-12 14:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'c7b41e0d5a92'
down_revision = 'a3e9c2f71b48'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_role",
        # The same key customs_job.owner_key holds.  Primary key outright: an
        # account has one role, not a history of them.
        sa.Column("owner_key", sa.String(length=160), primary_key=True),
        # Only "admin" is meaningful; anything else reads as no role at all, so a
        # typo cannot silently grant something.
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granted_by", sa.String(length=120), nullable=True),
    )
    op.create_table(
        "account_disabled",
        sa.Column("owner_key", sa.String(length=160), primary_key=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disabled_by", sa.String(length=120), nullable=True),
    )
    # No index beyond the primary key on either: every read is a single
    # primary-key lookup by owner_key.


def downgrade() -> None:
    op.drop_table("account_disabled")
    op.drop_table("account_role")
