"""Free documents wedged in EXTRACTING with nothing left to finish them.

    python scripts/release_stuck_extractions.py            # list them (default)
    python scripts/release_stuck_extractions.py --release  # put them back

worker.py's module docstring points here, and this is the case it means: with
queue mode OFF, an EXTRACTING claim is released by the process that took it,
and the API clears any leftovers at startup (services.recover_interrupted_
extractions).  Queue mode disables that sweep on purpose — a claim it found
would usually belong to a *live* worker on another machine, and clearing it
would let a second extraction start on the same document.  Redelivery is the
crash recovery instead: a worker that dies mid-run never deletes its message,
so SQS hands the document to the next worker.

That leaves exactly one hole, and it is why this script exists: a document
claimed while the queue was off (or claimed by a producer whose send failed in
a way that also lost the release) has no message behind it.  Nothing will ever
redeliver, no sweep runs, and the reviewer sees "already being extracted"
forever on an extraction that is not running.

THE RULE FOR RUNNING IT: stop the API and every worker first.

Not a formality.  This cannot tell a wedged claim from a healthy one — an
extraction that is genuinely in flight looks identical in the database.
Release one of those and the reviewer can press Continue while the first run is
still going: two concurrent paid extractions on one document, and the later
commit silently overwrites the earlier.  With everything stopped, every
EXTRACTING row is by definition stale.

What a release does (services.recover_interrupted_extractions): the document
goes back to UPLOADED, not FAILED — nothing failed and nothing is lost, since
stored OCR is reused when Continue runs again — carrying a warning that says so,
plus an audit event.  Any message still on the queue for it stays harmless: a
worker that receives one finds a non-EXTRACTING document and deletes it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select                    # noqa: E402

from app import services                         # noqa: E402
from app.config import get_settings              # noqa: E402
from app.database import SessionLocal, init_db   # noqa: E402
from app.domain.enums import DocumentStatus      # noqa: E402
from app.models import Document                  # noqa: E402


def _stuck(db) -> list[Document]:
    return list(db.scalars(
        select(Document)
        .where(Document.status == DocumentStatus.EXTRACTING.value)
        .order_by(Document.created_at)))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List (or release) documents stuck in EXTRACTING.")
    parser.add_argument("--release", action="store_true",
                        help="actually release them; without this the script "
                             "only reports what it would do")
    args = parser.parse_args()

    settings = get_settings()
    init_db()
    with SessionLocal() as db:
        stuck = _stuck(db)
        if not stuck:
            print("No documents are EXTRACTING — nothing is wedged.")
            return 0

        print(f"{len(stuck)} document(s) claimed as EXTRACTING:\n")
        for doc in stuck:
            print(f"  {doc.id}  {doc.declared_role} #{doc.upload_index_within_role}"
                  f"  job {doc.job_id}  {doc.original_file_name}"
                  f"  (claimed {doc.created_at:%Y-%m-%d %H:%M})")

        if not args.release:
            print("\nNothing was changed. If — and only if — the API and every "
                  "worker are STOPPED, re-run with --release.")
            if settings.queue_provider == "sqs":
                print("Queue mode is on: a running worker's in-flight document "
                      "looks exactly like this list. Check that no worker is "
                      "up before you release.")
            return 0

        released = services.recover_interrupted_extractions(db)
        print(f"\nReleased {len(released)} document(s) to UPLOADED. Each carries "
              f"an EXTRACTION_INTERRUPTED warning explaining why it went "
              f"backwards; the reviewer presses Continue to run it again "
              f"(stored OCR is reused, so no re-scan is paid for).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
