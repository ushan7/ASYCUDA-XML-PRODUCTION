"""give every pre-ownership job an owner, before unowned means invisible

``services.job_visible_to`` used to return True for a job with a blank
``owner_key``.  That was correct while exactly one account could exist: hiding a
broker's entire history behind a check only one person could ever match was the
worse of the two failures.  A second account can now exist, so the same branch
means "every user sees every unowned job" — the disclosure the check exists to
prevent — and it has been removed.

Which makes this revision load-bearing rather than tidy-up: the moment that
branch goes, every row still holding a blank owner_key becomes invisible to
everybody.  Those rows are a real broker's finalized declarations.

The owner they are assigned is the single configured operator, because on a
deployment that has one account that is who they belong to, by construction —
nobody else could have created them.  The SQLite path already backfills this way
(``database._migrate_sqlite``); this is the same statement for Postgres, and for
any SQLite file that predates that step.

REFUSES rather than guesses.  If there are unowned rows and no account is
configured to attribute them to, this raises instead of leaving them stranded:
a migration that silently makes a broker's declarations unreachable, and reports
success, is the failure mode worth being loud about.  The fix is either to set
EASYCUSTOMS_AUTH_USERNAME for the migration run, or to assign them deliberately
with scripts/reassign_job_owner.py.

Moving to Supabase Auth afterwards changes the owner_key of these rows from the
operator's NAME to their Supabase user id.  That is the same script's job, run
once at cutover — see its docstring.

Revision ID: e4f7a2c8b915
Revises: d15b8c47a06e
Created: 2026-08-11 03:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'e4f7a2c8b915'
down_revision = 'd15b8c47a06e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    unowned = conn.execute(sa.text(
        "SELECT COUNT(*) FROM customs_job "
        "WHERE owner_key IS NULL OR owner_key = ''")).scalar() or 0
    if not unowned:
        return

    # Imported here rather than at module scope: a revision that reads the
    # application's configuration should only do so when it has work to do.
    from app.auth import configured_username

    owner = configured_username()
    if not owner:
        raise RuntimeError(
            f"{unowned} job(s) have no owner_key, and no account is configured to "
            f"attribute them to.\n"
            f"From the revision after this one, a job with no owner is visible to "
            f"nobody — these would become unreachable declarations.\n"
            f"Either set EASYCUSTOMS_AUTH_USERNAME for this migration run, or assign "
            f"them deliberately first:\n"
            f"    cd backend && python scripts/reassign_job_owner.py --list")

    conn.execute(sa.text(
        "UPDATE customs_job SET owner_key = :owner "
        "WHERE owner_key IS NULL OR owner_key = ''"), {"owner": owner})


def downgrade() -> None:
    # Deliberately not reversed.  Blanking owner_key again would hand every job
    # back to every user on a database that now has more than one, and the
    # previous state (a mix of owned and unowned rows) is not recoverable from
    # here anyway — this revision cannot tell which rows it filled in.
    pass
