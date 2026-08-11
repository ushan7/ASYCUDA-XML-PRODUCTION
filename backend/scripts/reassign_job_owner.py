"""Move jobs from one owner_key to another.  Run at the Supabase cutover.

WHY THIS EXISTS

``Job.owner_key`` holds whoever may see a job.  On the local provider that is
the configured operator's NAME; on Supabase Auth it is that account's user id.
Those are different strings for the same human, so the day a deployment switches
provider, every job it already holds is owned by a principal that no longer
signs in — and since a job with an unmatched owner is visible to nobody, the
operator opens the dashboard to an empty list and their finalized declarations
appear to be gone.

Nothing can do this automatically: only a person knows that the Supabase account
``a1b2…`` is the same broker as the local account ``ushan``.  So it is a
deliberate step with a dry run, not a migration.

USAGE

    cd backend

    # 1. See who owns what.
    python scripts/reassign_job_owner.py --list

    # 2. Rehearse. Prints the count and changes nothing.
    python scripts/reassign_job_owner.py --from ushan --to a1b2c3d4-...

    # 3. Do it.
    python scripts/reassign_job_owner.py --from ushan --to a1b2c3d4-... --apply

Find the Supabase user id in the dashboard (Authentication → Users), or via the
admin API.  It is the ``id`` column, a UUID — not the email.

Stop the API first if you can.  It is not strictly required (this is a single
UPDATE and the rows are not being written by a signed-out user), but a reviewer
mid-session keeps a token naming the OLD principal until it expires, and will
see an empty dashboard until they sign in again.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select, update  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Job  # noqa: E402


def list_owners() -> int:
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Job.owner_key, func.count(Job.id))
            .group_by(Job.owner_key)
            .order_by(func.count(Job.id).desc())
        ).all()
        if not rows:
            print("no jobs in this database")
            return 0
        print(f"{'owner_key':<50} jobs")
        print("-" * 60)
        for owner, count in rows:
            shown = owner if owner else "(unowned — visible to nobody)"
            print(f"{shown:<50} {count}")
    finally:
        db.close()
    return 0


def reassign(old: str, new: str, *, apply: bool) -> int:
    if not new.strip():
        print("--to must name a principal; blanking an owner hides its jobs from "
              "everyone", file=sys.stderr)
        return 2
    db = SessionLocal()
    try:
        # An empty --from means the pre-ownership rows, which is a legitimate
        # thing to want: they are exactly what the backfill migration refuses to
        # guess at.
        criterion = (Job.owner_key.is_(None) | (Job.owner_key == "")) if not old \
            else (Job.owner_key == old)
        affected = db.scalar(select(func.count(Job.id)).where(criterion)) or 0
        label = old or "(unowned)"
        if not affected:
            print(f"no jobs owned by {label} — nothing to do")
            return 0
        if not apply:
            print(f"DRY RUN: {affected} job(s) would move from {label} to {new}")
            print("re-run with --apply to make the change")
            return 0
        db.execute(update(Job).where(criterion).values(owner_key=new))
        db.commit()
        print(f"moved {affected} job(s) from {label} to {new}")
    finally:
        db.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true",
                        help="show every owner_key and how many jobs it holds")
    parser.add_argument("--from", dest="old", default=None,
                        help="the current owner_key (empty string = unowned rows)")
    parser.add_argument("--to", dest="new", default=None,
                        help="the new owner_key (a Supabase user id at cutover)")
    parser.add_argument("--apply", action="store_true",
                        help="make the change; without it this is a dry run")
    args = parser.parse_args()

    if args.list:
        return list_owners()
    if args.old is None or args.new is None:
        parser.print_help()
        return 2
    return reassign(args.old, args.new, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
