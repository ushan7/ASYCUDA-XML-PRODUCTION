"""Test producer — put work on the queue by hand, without the SPA.

Run from backend/ with the same .env the API and the worker use:

    python producer.py --list                 # what can be enqueued right now
    python producer.py --document-id <id>     # enqueue one document, for real
    python producer.py --role INVOICE         # ...or the first claimable INVOICE
    python producer.py --probe                # connectivity only, no document
    python producer.py --probe unsupported    # exercise the DLQ path

In production the producer is not this file — it is app/queueing.py, driven by
POST /extract when EASYCUSTOMS_QUEUE_PROVIDER=sqs.  This is the manual handle
on the same machinery: a second terminal next to `python worker.py` so you can
watch a message be sent, long-polled, processed and deleted, without clicking
through the frontend.

WHY IT ENQUEUES THROUGH app/queueing.py RATHER THAN send_message
----------------------------------------------------------------
Because a bare send does not test anything.  The claim is the design (see
app/queueing.py): the producer moves the document to EXTRACTING *before* the
message exists, and the worker only runs documents it finds EXTRACTING.  A
message sent without a claim therefore reaches a worker that correctly decides
there is nothing to do and deletes it — a green log line for a pipeline that
never ran an extraction.  Calling the real producer means what you watch is
what production does, including the claim, the audit event and the release on a
failed send.

`--probe` is the exception, and is honest about it: it sends a message naming
ids that do not exist, purely to prove that credentials, region, queue URL and
long polling work.  The worker WILL log "no longer exists — message deleted",
and that is the pass condition.

MESSAGE SHAPE
-------------
The body is the v1 schema in app/queueing.py — {v, kind, job_id, document_id,
actor} — and this script deliberately does not invent its own.  If you are
looking for the file_url/user_id shape from an early sketch: the file is not in
the message because documents live in shared storage that both processes reach
(and an SQS body is capped at 256 KB), so the message carries a document_id
reference; and the user is `actor`, the operator name that every audited write
is attributed to.  Changing the wire format means bumping MESSAGE_V and
teaching worker.parse_message about it — not editing a test script.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from sqlalchemy import select

from app import queueing
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.domain.enums import DocumentStatus
from app.domain.errors import BlockingValidationError
from app.models import Document

log = logging.getLogger("easycustoms.producer")

# The statuses claim_for_queued_extraction will accept.  Anything else is
# already running, already done, or rejected.
CLAIMABLE = (DocumentStatus.UPLOADED.value, DocumentStatus.FAILED.value)

PROBES = ("orphan", "malformed", "unsupported")


def _claimable(db, role: str | None) -> list[Document]:
    stmt = select(Document).where(Document.status.in_(CLAIMABLE))
    if role:
        stmt = stmt.where(Document.declared_role == role.upper())
    return list(db.scalars(stmt.order_by(Document.created_at.desc())))


def _describe(doc: Document) -> str:
    return (f"{doc.id}  {doc.status:<9} {doc.declared_role} "
            f"#{doc.upload_index_within_role}  job {doc.job_id}  "
            f"{doc.original_file_name}")


def list_documents(role: str | None) -> int:
    with SessionLocal() as db:
        docs = _claimable(db, role)
        if not docs:
            print("Nothing is claimable. Upload a document first — only "
                  "UPLOADED and FAILED documents can be enqueued (anything else "
                  "is already running, already extracted, or rejected).")
            return 1
        print(f"{len(docs)} claimable document(s), newest first:\n")
        for doc in docs:
            print(f"  {_describe(doc)}")
        print("\nEnqueue one with:  python producer.py --document-id <id>")
    return 0


def enqueue(document_id: str | None, role: str | None, actor: str) -> int:
    with SessionLocal() as db:
        if document_id:
            doc = db.get(Document, document_id)
            if doc is None:
                print(f"No document {document_id!r}. Try --list.", file=sys.stderr)
                return 1
            if doc.status not in CLAIMABLE:
                print(f"Document {document_id} is {doc.status}, and only "
                      f"{' or '.join(CLAIMABLE)} can be enqueued.", file=sys.stderr)
                return 1
        else:
            candidates = _claimable(db, role)
            if not candidates:
                print("Nothing claimable to enqueue. Try --list.", file=sys.stderr)
                return 1
            doc = candidates[0]
            print(f"Picked the newest claimable document:\n  {_describe(doc)}\n")
        try:
            queueing.enqueue_extraction(db, doc, actor=actor)
        except BlockingValidationError as e:
            # Someone (or something) claimed it between the check and the call.
            print(f"Refused: {e}", file=sys.stderr)
            return 1
        except queueing.QueueSendError as e:
            # The claim has already been rolled back by the producer.
            print(f"Send failed: {e}\nThe document was released and is "
                  f"claimable again.", file=sys.stderr)
            return 1
    print(f"Queued document {doc.id} (job {doc.job_id}) as {actor!r}.\n"
          f"The document is now EXTRACTING; the worker takes it from there.\n"
          f"Watch the other terminal, or `journalctl -u easycustoms-worker -f`.")
    return 0


def probe(kind: str, settings) -> int:
    """Send a message that references no real work.

    Three shapes, one per branch of worker.parse_message, so each disposal path
    can be watched on a queue that is otherwise empty:

      orphan       well-formed, ids that do not exist -> worker deletes it
                   ("no longer exists"). Proves credentials/region/URL/polling.
      malformed    not JSON at all -> worker logs the body and deletes it.
      unsupported  a future `v` -> the worker deliberately does NOT delete it
                   (deleting would strand a newer producer's claim), so it
                   redelivers until maxReceiveCount sends it to the DLQ. This
                   is the one probe that leaves a trace: run it to prove the
                   redrive policy works, then check the DLQ, and expect the
                   worker to log a rejection once per delivery until it moves.
    """
    if kind == "malformed":
        body = "this is not json"
    elif kind == "unsupported":
        body = json.dumps({"v": queueing.MESSAGE_V + 1,
                           "kind": queueing.KIND_EXTRACT_DOCUMENT,
                           "job_id": "probe", "document_id": "probe",
                           "actor": "producer.py --probe"})
    else:
        body = json.dumps({"v": queueing.MESSAGE_V,
                           "kind": queueing.KIND_EXTRACT_DOCUMENT,
                           "job_id": "probe-job-does-not-exist",
                           "document_id": "probe-document-does-not-exist",
                           "actor": "producer.py --probe"})
    client = queueing.make_sqs_client(settings)
    resp = client.send_message(QueueUrl=settings.sqs_queue_url, MessageBody=body)
    print(f"Sent {kind} probe: MessageId={resp.get('MessageId')}")
    if kind == "unsupported":
        print("The worker will refuse this once per delivery and leave it on "
              "the queue; after maxReceiveCount deliveries it should appear in "
              "customs-processing-dlq. Nothing else will clear it.")
    else:
        print("Expect the worker to delete it without extracting anything — "
              "that IS the pass condition for a connectivity probe.")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        description="Put extraction work on the SQS queue by hand.")
    parser.add_argument("--list", action="store_true",
                        help="list documents that can be enqueued, then exit")
    parser.add_argument("--document-id", help="the document to enqueue")
    parser.add_argument("--role", help="with no --document-id, pick the newest "
                                       "claimable document of this role "
                                       "(INVOICE, PACKING_LIST, ...)")
    parser.add_argument("--actor", default="producer.py",
                        help="operator name recorded on the audit trail "
                             "(default: producer.py)")
    parser.add_argument("--probe", nargs="?", const="orphan", choices=PROBES,
                        help="send a message that references no real document, "
                             "to test the wire rather than the pipeline")
    args = parser.parse_args()

    settings = get_settings()
    # --list only reads the database, so it works before a queue exists —
    # useful when you are still deciding what to point at one.
    if args.list:
        init_db()
        return list_documents(args.role)

    if not settings.sqs_queue_url.strip():
        print("SQS_QUEUE_URL is not set in backend/.env — there is no queue to "
              "send to. See .env.example, or run "
              "scripts/provision_sqs.py --apply.", file=sys.stderr)
        return 2
    if settings.queue_provider != "sqs":
        # Worth sending anyway: the wire is testable before the flag is flipped.
        # The worker is not, so say so plainly.
        print(f"warning: EASYCUSTOMS_QUEUE_PROVIDER is "
              f"{settings.queue_provider!r}, so worker.py will refuse to start "
              f"and nothing will consume this message.\n", file=sys.stderr)

    if args.probe:
        return probe(args.probe, settings)

    init_db()
    return enqueue(args.document_id, args.role, args.actor)


if __name__ == "__main__":
    raise SystemExit(main())
