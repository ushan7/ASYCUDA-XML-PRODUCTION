"""record what each extraction spends, and whose spend it is

Adds ``usage_event``.

Every extraction buys Mistral OCR and OpenAI tokens, and nothing recorded who
bought them.  The counts existed — ``OpenAIExtractor`` has always accumulated
them — but only to be written to a log line and discarded, so at 1500 users
there was no answer to "what does a job cost", "which account is burning the
budget", or "is anyone abusing this".  Metering has to exist before billing can
mean anything, which is why it is its own step.

Units are authoritative and always recorded; the money column is an estimate
computed from a configurable rate table, NULL when no rate is known for that
model, and stamped with the rate version that produced it so a later price
change cannot silently rewrite what last month appeared to cost.

``owner_key`` is COPIED onto the row rather than joined from customs_job at read
time: reassigning a job later (which the Supabase cutover does to every row)
must not move last month's cost onto a different account.

No backfill.  Past spend is not recoverable — the token counts for it were only
ever logged, and inventing rows for jobs whose real cost is unknown would put
numbers into a cost report that nothing can reconcile.

Revision ID: f281d6b04a37
Revises: e4f7a2c8b915
Created: 2026-08-11 04:30:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'f281d6b04a37'
down_revision = 'e4f7a2c8b915'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_event",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("owner_key", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("operation", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pages", sa.Integer(), nullable=False, server_default="0"),
        # Decimal-as-string, like customs_job.exchange_rate: money never touches
        # a float here. NULL means "no rate configured", which is a different
        # fact from zero and must not be reported as free.
        sa.Column("estimated_cost_usd", sa.String(length=40), nullable=True),
        sa.Column("rate_version", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_usage_event_owner_created", "usage_event",
                    ["owner_key", "created_at"])
    op.create_index("ix_usage_event_job", "usage_event", ["job_id"])
    op.create_index("ix_usage_event_created_at", "usage_event", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_usage_event_created_at", table_name="usage_event")
    op.drop_index("ix_usage_event_job", table_name="usage_event")
    op.drop_index("ix_usage_event_owner_created", table_name="usage_event")
    op.drop_table("usage_event")
