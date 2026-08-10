"""move the login throttle out of process memory

Adds ``login_attempt``, replacing the ``auth._failures`` dict.

A per-process counter is not a rate limit once there is more than one process:
N processes each keep their own count, so the deployment allows N times the
intended guess rate, and whichever process the load balancer happens to pick
decides whether a caller is blocked. It was the last thing pinning the API to
``--workers 1``.

The table is a short SLIDING WINDOW, not a login history: successful sign-ins
delete the caller's rows and anything past the window is pruned on write.
Nothing durable belongs here — ``audit_event`` is where that goes.

Deliberately no backfill and no downgrade data: the previous counts lived in
process memory and are gone the moment the old process stops, which is the
correct starting state for a rate limit anyway.

Note that this table alone does not fix the throttle behind a proxy — there
every request arrives from the proxy, so all callers would share one key and
one bucket. ``auth.client_key`` + ``EASYCUSTOMS_TRUSTED_PROXY_HOPS`` is the
other half, and neither half is any use without the other.

Revision ID: d15b8c47a06e
Revises: c93a5d21e4b8
Created: 2026-08-11 01:30:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'd15b8c47a06e'
down_revision = 'c93a5d21e4b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_attempt",
        sa.Column("id", sa.String(length=36), primary_key=True),
        # An IP address; 45 characters covers IPv6 including a v4-mapped form.
        sa.Column("client_key", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Both columns, in this order: every read is "this caller, inside the
    # window", and the prune is a range scan on the same index.
    op.create_index("ix_login_attempt_client_created", "login_attempt",
                    ["client_key", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_login_attempt_client_created", table_name="login_attempt")
    op.drop_table("login_attempt")
