"""let a user recover an account, and count that separately again

Adds ``password_reset_attempt``, the abuse limiter for
``POST /api/auth/password-reset``.

A THIRD table, and for the same reason there is a second one.  It cannot share
``signup_attempt``: three password resets would then exhaust a caller's
registration budget and three registrations would stop them recovering an
account, and ``auth.reset_signup_limiter`` — the manual unlock for a caller
stranded behind a load balancer at ``trusted_proxy_hops=0`` — would empty a
window nobody asked it about.  It cannot share ``login_attempt``, which counts
failures, prunes at five minutes and is emptied by a correct password.  Limiters
that must not clear one another are limiters that do not share a table.

It counts EVERY request this app passed to the identity provider, whatever the
provider then answered.  Counting only the ones that produced an email would make
*how many tries you have left* vary by whether the address exists, which is the
account-existence oracle re-entering through the budget after the status code
closed the front door.  For the same reason the row holds no email, no user id
and no outcome — a caller key and a timestamp.

NO BACKFILL: a sliding window starts empty by definition.

Nothing here is read by ``services.job_visible_to``, and nothing changes about
who may see a declaration.

Revision ID: d0e93b7c4f18
Revises: b6f4c1a70e35
Created: 2026-08-13 15:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'd0e93b7c4f18'
down_revision = 'b6f4c1a70e35'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "password_reset_attempt",
        sa.Column("id", sa.String(length=36), primary_key=True),
        # An IP address, from `auth.client_key` — the same key the login throttle
        # and the signup limiter use, so EASYCUSTOMS_TRUSTED_PROXY_HOPS governs
        # all three.
        sa.Column("client_key", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Every read is "this caller, this window", which is exactly this index.
    op.create_index("ix_password_reset_attempt_client_created", "password_reset_attempt",
                    ["client_key", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_password_reset_attempt_client_created",
                  table_name="password_reset_attempt")
    op.drop_table("password_reset_attempt")
