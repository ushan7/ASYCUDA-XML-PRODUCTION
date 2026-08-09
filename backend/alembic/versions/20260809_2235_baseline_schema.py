"""baseline schema

The schema as it stood when migrations were introduced (2026-08-09) — five
tables and the ownership index, exactly what ``Base.metadata.create_all`` had
been building until now.

TWO WAYS TO ARRIVE HERE, and they are not interchangeable:

  * A NEW database (the Postgres cutover, a fresh Docker volume):
        alembic upgrade head
    runs this revision and creates the tables.

  * An EXISTING database that predates Alembic — the SQLite file a broker has
    been using, or a Postgres database already built by create_all:
        alembic stamp fa843d18c61e
    records "this database is already at the baseline" WITHOUT running the
    CREATE TABLEs, which would fail against tables that exist. Then
    ``alembic upgrade head`` applies everything after it.

Getting that backwards is the one dangerous mistake here, which is why
app/database.py's check names the stamp command in its error rather than
telling you to run an upgrade that cannot work.

Types are deliberately the portable SQLAlchemy ones (String/JSON/DateTime/
LargeBinary), not dialect types: the same revision has to build a SQLite file
on a broker's laptop and a Postgres database on EC2.

Revision ID: fa843d18c61e
Revises:
Created: 2026-08-09 22:35:20.081223
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'fa843d18c61e'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'customs_job',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('rule_set_version', sa.String(length=40), nullable=False),
        sa.Column('exchange_rate', sa.String(length=40), nullable=False),
        sa.Column('created_by', sa.String(length=120), nullable=False),
        sa.Column('owner_key', sa.String(length=160), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('critical_review', sa.JSON(), nullable=True),
        sa.Column('declaration', sa.JSON(), nullable=True),
        sa.Column('item_mutations', sa.JSON(), nullable=True),
        sa.Column('review_selections', sa.JSON(), nullable=True),
        sa.Column('hs_history', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    # Every job listing filters by owner: without this the ownership seam turns
    # every history page into a full scan.
    op.create_index('ix_customs_job_owner_key', 'customs_job', ['owner_key'],
                    unique=False)

    op.create_table(
        'uploaded_document',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('job_id', sa.String(length=36), nullable=False),
        sa.Column('declared_role', sa.String(length=40), nullable=False),
        sa.Column('upload_index_within_role', sa.Integer(), nullable=False),
        sa.Column('original_file_name', sa.String(length=400), nullable=False),
        sa.Column('content_type', sa.String(length=120), nullable=False),
        sa.Column('byte_size', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('storage_key', sa.String(length=400), nullable=False),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('ocr', sa.JSON(), nullable=True),
        sa.Column('raw_extraction', sa.JSON(), nullable=True),
        sa.Column('role_match', sa.Boolean(), nullable=True),
        sa.Column('warnings', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['customs_job.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'xml_artifact',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('job_id', sa.String(length=36), nullable=False),
        sa.Column('declaration_version', sa.Integer(), nullable=False),
        sa.Column('template_version', sa.String(length=40), nullable=False),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.Column('xml_bytes', sa.LargeBinary(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['customs_job.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'bms_artifact',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('job_id', sa.String(length=36), nullable=False),
        sa.Column('declaration_version', sa.Integer(), nullable=False),
        sa.Column('template_version', sa.String(length=40), nullable=False),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.Column('xls_bytes', sa.LargeBinary(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['customs_job.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'audit_event',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('job_id', sa.String(length=36), nullable=False),
        sa.Column('actor', sa.String(length=120), nullable=False),
        sa.Column('event_code', sa.String(length=80), nullable=False),
        sa.Column('detail', sa.Text(), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['customs_job.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Drops every table — i.e. every job, document, artifact and audit event.

    Kept because a baseline with no downgrade cannot be tested, not because
    running it on a real database is ever the answer.
    """
    op.drop_table('audit_event')
    op.drop_table('bms_artifact')
    op.drop_table('xml_artifact')
    op.drop_table('uploaded_document')
    op.drop_index('ix_customs_job_owner_key', table_name='customs_job')
    op.drop_table('customs_job')
