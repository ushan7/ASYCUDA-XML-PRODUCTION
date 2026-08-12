"""Make an account a platform administrator, or take it back.

WHY THIS IS A SCRIPT AND NOT A ROUTE

The first admin cannot be granted through a route that requires an admin, and
there is no non-silly answer to that: any HTTP path that could create the first
one is a path that can create the second one without an existing admin's
involvement.  So the bootstrap is out of band, once, from a shell on the box that
holds the database — and once it is out of band there is no reason for a
privileged "promote this account" endpoint to exist at all.  An admin session can
set quotas and bar accounts; it cannot mint more admins.

WHAT THE ROLE IS, before you grant it.  `docs/ADR-001-identity-and-tenancy.md`:
an admin manages accounts, quotas and usage METADATA.  An admin CANNOT read
customer declarations, documents, extractions, XML artifacts or job audit trails
— `services.job_visible_to` does not know the role exists, and the isolation
suite asserts that an admin gets the same 404 as anyone else on a job they do not
own.  `admin` is a PLATFORM role (the service operator), never a firm role: it
does not mean "senior broker".

USAGE

    cd backend

    # 1. Who is an admin now, and which accounts exist to choose from.
    python scripts/grant_admin.py --list

    # 2. Rehearse. Prints what would change and changes nothing.
    python scripts/grant_admin.py --grant a1b2c3d4-...

    # 3. Do it.
    python scripts/grant_admin.py --grant a1b2c3d4-... --apply

    # ...and the way back.
    python scripts/grant_admin.py --revoke a1b2c3d4-... --apply

The key is `auth.users.id` from Supabase (Authentication -> Users; the `id`
column, a UUID — not the email), or the configured username under the `local`
provider.  It is the same value `customs_job.owner_key` holds, so
`--list` shows the keys this deployment has actually seen, and a key that is not
in that list is either a brand-new account or a typo — this app has no user table
to check it against.

Revoking takes effect on that account's NEXT request: the role is read from the
table per request on the admin routes, never carried in the 24h session token,
precisely so that revocation does not have to wait for a token to expire.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app import accounts  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.models import AccountDisabled, AccountRole, Job  # noqa: E402


def _list() -> int:
    db = SessionLocal()
    try:
        admins = db.scalars(select(AccountRole).order_by(AccountRole.owner_key)).all()
        print("administrators")
        print("-" * 78)
        if not admins:
            print("  (none — nobody can reach /api/admin/* on this deployment)")
        for row in admins:
            when = row.granted_at.isoformat() if row.granted_at else "?"
            print(f"  {row.owner_key:<40} {row.role:<8} granted {when}"
                  f"{' by ' + row.granted_by if row.granted_by else ''}")

        barred = {r.owner_key for r in db.scalars(select(AccountDisabled)).all()}
        print()
        print("accounts this deployment has seen (owner_key, jobs)")
        print("-" * 78)
        rows = db.execute(
            select(Job.owner_key, func.count(Job.id))
            .where(Job.owner_key != "")
            .group_by(Job.owner_key)
            .order_by(func.count(Job.id).desc())).all()
        if not rows:
            print("  (no jobs yet — an account with no jobs is not listed here; identity "
                  "lives with the auth provider)")
        for owner, count in rows:
            marks = []
            if accounts.is_admin(db, owner):
                marks.append("admin")
            if owner in barred:
                marks.append("DISABLED")
            print(f"  {owner:<40} {count:>5}  {' '.join(marks)}")
    finally:
        db.close()
    return 0


def _grant(owner_key: str, *, apply: bool, actor: str | None) -> int:
    db = SessionLocal()
    try:
        if accounts.is_admin(db, owner_key):
            print(f"{owner_key} is already an administrator — nothing to do")
            return 0
        if not accounts.has_activity(db, owner_key):
            # Not refused: an account can legitimately be promoted before it has
            # uploaded anything.  Said out loud, because this app cannot check a
            # key against the identity provider and a typo would otherwise create
            # an administrator nobody can sign in as.
            print(f"NOTE: {owner_key} has no jobs and no usage in this database. That is "
                  f"fine for a new account and is what a typo looks like — check it "
                  f"against Supabase (Authentication -> Users, the id column).")
        if not apply:
            print(f"DRY RUN: {owner_key} would become an administrator")
            print("re-run with --apply to make the change")
            return 0
        accounts.grant_admin(db, owner_key, granted_by=actor)
        db.commit()
        print(f"{owner_key} is now an administrator (accounts, quotas and usage metadata "
              f"only — not declarations)")
    finally:
        db.close()
    return 0


def _revoke(owner_key: str, *, apply: bool) -> int:
    db = SessionLocal()
    try:
        if not accounts.is_admin(db, owner_key):
            print(f"{owner_key} is not an administrator — nothing to do")
            return 0
        remaining = [r.owner_key for r in db.scalars(select(AccountRole)).all()
                     if r.owner_key != owner_key and accounts.is_admin(db, r.owner_key)]
        if not remaining:
            # Not refused — a deployment is allowed to have no administrator, and
            # this script is how it gets one back.  But it is worth knowing that
            # the admin routes are about to answer 404 to everybody.
            print("NOTE: this is the last administrator. After this, /api/admin/* answers "
                  "404 to every account until this script grants the role again.")
        if not apply:
            print(f"DRY RUN: {owner_key} would stop being an administrator")
            print("re-run with --apply to make the change")
            return 0
        accounts.revoke_admin(db, owner_key)
        db.commit()
        print(f"{owner_key} is no longer an administrator (effective on their next request)")
    finally:
        db.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true",
                        help="show the administrators and the accounts this deployment has seen")
    parser.add_argument("--grant", metavar="OWNER_KEY", default=None,
                        help="the account to make an administrator")
    parser.add_argument("--revoke", metavar="OWNER_KEY", default=None,
                        help="the account to take the role back from")
    parser.add_argument("--by", default=None,
                        help="who is granting it, recorded in account_role.granted_by")
    parser.add_argument("--apply", action="store_true",
                        help="make the change; without it this is a dry run")
    args = parser.parse_args()

    # A pre-alembic or brand-new SQLite file has no account_role table yet, and
    # the first thing this script would do is query it.  init_db is what the API
    # runs at startup; on Postgres it verifies the schema is at head instead.
    init_db()

    if args.list:
        return _list()
    if args.grant and args.revoke:
        print("--grant and --revoke are opposites; pass one", file=sys.stderr)
        return 2
    if args.grant:
        return _grant(args.grant.strip(), apply=args.apply, actor=args.by)
    if args.revoke:
        return _revoke(args.revoke.strip(), apply=args.apply)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
