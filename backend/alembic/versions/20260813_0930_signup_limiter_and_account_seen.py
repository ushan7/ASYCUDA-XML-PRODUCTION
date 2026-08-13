"""let strangers register, and count it separately from failed sign-ins

Adds ``signup_attempt`` and ``account_seen``.

``signup_attempt`` is the abuse limiter for ``POST /api/auth/signup``.  It is a
SEPARATE TABLE from ``login_attempt`` and not a ``kind`` column on it, which is
the control rather than a preference.  The login throttle counts FAILURES and a
correct password deletes that caller's rows; signup abuse is a stream of
SUCCESSES.  Sharing the table means either a successful signup clears the
caller's password-guessing budget — so an attacker interleaves a registration
between guesses and guesses for ever — or the two share a pruning delete and the
24h signup window is silently truncated to the 5-minute failure window.  Two
tables cannot do either to each other.

``account_seen`` records that an account has SIGNED IN, and holds the only email
address in this schema.  Step 2's admin listing is the union of the ``owner_key``
values in the app's own tables, so it can say who is spending the budget and
cannot say whether a given person ever got in, or which UUID belongs to
alice@broker.np — both of which become support questions the day registration
opens.  Written at SIGN-IN and never at signup: with email confirmation on,
Supabase answers an already-registered address with an obfuscated user whose id
belongs to nobody, so a row written from a signup response would key on a
fabricated id and a stranger could fill this table with them.

NO BACKFILL for either.  A signup window is a sliding window and starts empty by
definition; ``account_seen`` fills itself as existing accounts sign in, and until
they do they remain exactly as visible to the admin listing as they were before
this revision — through their jobs and their usage.

Neither table is read by ``services.job_visible_to``, and nothing here changes
who may see a declaration.

Revision ID: b6f4c1a70e35
Revises: c7b41e0d5a92
Created: 2026-08-13 09:30:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'b6f4c1a70e35'
down_revision = 'c7b41e0d5a92'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signup_attempt",
        sa.Column("id", sa.String(length=36), primary_key=True),
        # An IP address, from `auth.client_key` — the same key the login throttle
        # uses, so EASYCUSTOMS_TRUSTED_PROXY_HOPS governs both.
        sa.Column("client_key", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Every read is "this caller, this window", which is exactly this index.
    op.create_index("ix_signup_attempt_client_created", "signup_attempt",
                    ["client_key", "created_at"])
    op.create_table(
        "account_seen",
        # The same key customs_job.owner_key holds.  Primary key outright: an
        # account is seen or it is not, and this is not a login history.
        sa.Column("owner_key", sa.String(length=160), primary_key=True),
        # What they signed in AS.  A display label, never an identity.
        sa.Column("display", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("account_seen")
    op.drop_index("ix_signup_attempt_client_created", table_name="signup_attempt")
    op.drop_table("signup_attempt")
