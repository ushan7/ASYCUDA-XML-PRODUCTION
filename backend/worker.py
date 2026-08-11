"""SQS worker — the consumer half of queue-mode extraction.

Run from backend/ with the same .env the API uses:

    python worker.py

The producer (app/queueing.py, driven by POST /extract when
EASYCUSTOMS_QUEUE_PROVIDER=sqs) has already CLAIMED the document
(status EXTRACTING) before its message exists.  One rule follows from that
and decides everything here: A MESSAGE DIES WITH ITS ATTEMPT.

  * Success, refusal, skip -> the message is deleted.
  * Extraction failure     -> run_extraction has already persisted FAILED and
    the reason; the message is deleted TOO.  The retry is the reviewer's
    Continue button (a fresh claim, a fresh message), exactly as in
    synchronous mode — never an SQS redelivery.  Auto-retrying here while the
    SPA shows FAILED with a retry button would race the human: two live
    messages for one document, two concurrent paid extractions, last writer
    silently winning.  Transient vendor hiccups are already retried inside
    the pipeline (429 backoff); an outage long enough to fail a run is one a
    human should look at anyway.
  * The ONLY undeleted messages are those whose worker DIED mid-run (nothing
    heartbeated, nothing deleted).  SQS re-delivers them and the next worker
    takes the claim over — redelivery IS the crash recovery, replacing the
    single-process startup sweep (services.recover_interrupted_extractions),
    which queue mode disables.  After maxReceiveCount such deliveries the
    queue moves the message to the DLQ, so the DLQ holds exactly the poison
    documents that kill workers — the thing it exists for.

So the states a delivery can find its document in:

    EXTRACTING   the producer's claim waiting for us, or a dead worker's —
                 either way, ours now: run it (claim_from=(EXTRACTING,)).
    anything else
                 done elsewhere, failed (human will retry), released, or the
                 job was deleted: delete the message, touch nothing.

While a document is being processed, a heartbeat thread re-extends the
message's invisibility, and every receive names its own VisibilityTimeout so
the head of the run never depends on how the queue was created.  Known,
accepted residue: if the heartbeat itself fails (SQS unreachable) while the
worker is alive and mid-run, a second worker can take the document over and
both commit — a duplicate spend on one document, last writer wins, logged
loudly on both sides.

Changing EASYCUSTOMS_QUEUE_PROVIDER: stop the API and every worker FIRST,
flip the flag, start again.  A document left EXTRACTING by a crash while the
queue was OFF has no message, so no worker will ever release it — run
scripts/release_stuck_extractions.py (with everything stopped) if one is
wedged.

Deployment (EC2 + systemd): see backend/deploy/easycustoms-worker.service.
On EC2 prefer an instance role over keys in .env — boto3 finds it by itself;
the AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY pair in backend/.env is the
dev-machine fallback (read via Settings, passed to the client explicitly —
a .env line never reaches os.environ, so boto3 alone would not see it).
"""
from __future__ import annotations

import json
import logging
import signal
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

from app import services
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.domain.enums import DocumentStatus
from app.domain.errors import BlockingValidationError
from app.models import Document
from app import logging_setup
from app.queueing import KIND_EXTRACT_DOCUMENT, MESSAGE_V, make_sqs_client

log = logging.getLogger("easycustoms.worker")

# The one status a delivered message may take the document from: the claim
# its producer took (or a dead worker left behind — same thing by the time
# SQS re-delivers).  Deliberately NOT FAILED: a failed attempt deletes its
# message, so a FAILED document with a live message means only that a delete
# was lost mid-crash — and the human retry that FAILED invites will mint its
# own claim and message.  Running here too would race it.
CLAIM_FROM = (DocumentStatus.EXTRACTING.value,)

_stop = threading.Event()


def parse_message(body: str) -> tuple[str, dict[str, str] | None]:
    """('ok', payload) | ('malformed', None) | ('unsupported', None).

    The split matters at disposal time: malformed bodies reference nothing
    recoverable, so they are deleted-with-a-full-log (the log line is the
    forensic record).  A WELL-FORMED message this build merely does not
    understand — a newer `v` or `kind` mid rolling upgrade — is left alone:
    deleting it would strand its producer's claim as EXTRACTING forever,
    while leaving it lets an upgraded worker (or the DLQ) have it.
    """
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return "malformed", None
    if not isinstance(data, dict):
        return "malformed", None
    if data.get("v") != MESSAGE_V or data.get("kind") != KIND_EXTRACT_DOCUMENT:
        return "unsupported", None
    job_id, document_id = data.get("job_id"), data.get("document_id")
    if not (isinstance(job_id, str) and job_id and
            isinstance(document_id, str) and document_id):
        return "malformed", None
    actor = data.get("actor")
    request_id = data.get("request_id")
    return "ok", {"job_id": job_id, "document_id": document_id,
                  "actor": actor if isinstance(actor, str) and actor else "worker",
                  "request_id": request_id if isinstance(request_id, str) else ""}


class Heartbeat(threading.Thread):
    """Re-extends one in-flight message's invisibility until stopped.

    The receive itself set VisibilityTimeout=`extend_to`, so the head of the
    run is covered before this thread's first beat; each beat then pushes the
    horizon out again.  Stop-then-join before disposing of the message — a
    beat that fired after a delete would matter to nobody, but joining makes
    the ordering a fact instead of a race.
    """

    def __init__(self, sqs: Any, queue_url: str, receipt: str, *,
                 every: int, extend_to: int, label: str) -> None:
        super().__init__(daemon=True, name=f"heartbeat:{label}")
        self._sqs, self._queue_url, self._receipt = sqs, queue_url, receipt
        self._every, self._extend_to, self._label = every, extend_to, label
        self._stopped = threading.Event()
        self.lost = False

    def run(self) -> None:
        while not self._stopped.wait(self._every):
            if self._stopped.is_set():
                return
            try:
                self._sqs.change_message_visibility(
                    QueueUrl=self._queue_url, ReceiptHandle=self._receipt,
                    VisibilityTimeout=self._extend_to)
            except Exception as e:
                # The message may already be visible to (or owned by) another
                # worker: keep working — our commit is still wanted — but say
                # loudly that a duplicate run is now possible.
                self.lost = True
                log.error("heartbeat lost for %s (%s) — the message may be "
                          "re-delivered and a second worker may duplicate this "
                          "extraction: %s", self._label, type(e).__name__, e)
                return

    def finish(self) -> None:
        self._stopped.set()
        self.join(timeout=30)


def _delete(sqs: Any, queue_url: str, msg: dict, *, label: str) -> None:
    try:
        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
    except Exception as e:
        # The receipt can expire under a very long run; the redelivered
        # duplicate will find a non-EXTRACTING status and be deleted then.
        log.warning("delete_message failed for %s (a duplicate delivery will "
                    "clean itself up): %s", label, e)


def process_message(sqs: Any, settings: Any, msg: dict) -> None:
    """Handle one delivery end-to-end.  Never raises.  Every outcome deletes
    the message except an unsupported schema (left for an upgraded worker /
    the DLQ) — see the module docstring for why failure is not an exception
    to that."""
    receive_count = int(msg.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
    state, payload = parse_message(msg.get("Body", ""))
    if state == "malformed":
        log.error("unintelligible message deleted (delivery %s): %.2000s",
                  receive_count, msg.get("Body", ""))
        _delete(sqs, settings.sqs_queue_url, msg, label="malformed")
        return
    if state == "unsupported":
        log.error("message with unknown v/kind left for an upgraded worker or "
                  "the DLQ (delivery %s): %.500s", receive_count, msg.get("Body", ""))
        return
    label = f"job {payload['job_id']} doc {payload['document_id']}"
    if receive_count > 1:
        log.warning("redelivery %s of %s — its previous worker died mid-run; "
                    "taking the claim over", receive_count, label)
    heartbeat = Heartbeat(sqs, settings.sqs_queue_url, msg["ReceiptHandle"],
                          every=settings.sqs_heartbeat_seconds,
                          extend_to=settings.sqs_visibility_extend_seconds,
                          label=label)
    heartbeat.start()
    # Adopt the enqueuing request's id, so this extraction's lines select
    # with the upload that caused it. Falls back to the document id, which
    # is still better than nothing for a redelivery.
    #
    # RESET in the finally, because these run on a reused pool thread: a
    # ContextVar set here outlives the message and the next one to be handled
    # by the same thread would log under the previous document's id if it
    # failed before reaching this line.
    id_token = logging_setup.request_id_var.set(
        payload["request_id"] or payload["document_id"])
    session = SessionLocal()
    try:
        doc = session.get(Document, payload["document_id"])
        if doc is None or doc.job_id != payload["job_id"]:
            log.info("%s no longer exists (job deleted?) — message deleted", label)
        elif doc.status not in CLAIM_FROM:
            # Done elsewhere, FAILED (the human retries), or a released
            # claim — see module docstring.
            log.info("%s is %s — nothing to do, message deleted", label, doc.status)
        else:
            log.info("extracting %s (%s #%s, delivery %s)", label,
                     doc.declared_role, doc.upload_index_within_role, receive_count)
            services.run_extraction(session, doc, actor=payload["actor"],
                                    claim_from=CLAIM_FROM)
            if heartbeat.lost:
                log.error("%s finished AFTER its heartbeat was lost — if a "
                          "second worker took it over, the later commit won", label)
            log.info("done: %s -> %s", label, doc.status)
    except BlockingValidationError as e:
        # From the claim: someone else finished it first (their result owns
        # the document).  From inside the run (e.g. the page ceiling): the
        # document is FAILED with the reason, and the reviewer decides.
        # Either way this attempt is over: the message goes.
        log.info("%s refused (%s) — message deleted", label, e)
    except Exception as e:
        # run_extraction persisted FAILED + the reason before re-raising; the
        # SPA shows it and Continue is the retry (a NEW claim + message).
        log.exception("extraction failed for %s (delivery %s) — the reviewer "
                      "retries via Continue: %s", label, receive_count, e)
    finally:
        try:
            session.rollback()   # no-op on clean sessions
        finally:
            session.close()
        heartbeat.finish()       # join BEFORE disposing of the message
        _delete(sqs, settings.sqs_queue_url, msg, label=label)
        logging_setup.request_id_var.reset(id_token)


def _install_signal_handlers() -> None:
    def _request_stop(signum: int, _frame: Any) -> None:
        log.info("signal %s: draining — no new receives, in-flight extractions "
                 "finish, undeleted messages simply re-deliver", signum)
        _stop.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)


def main() -> int:
    settings = get_settings()
    # Same JSON shape and the same redaction as the API: a fleet whose two
    # halves log differently cannot be queried as one system.
    logging_setup.configure_logging(settings)
    if settings.queue_provider != "sqs":
        log.error("EASYCUSTOMS_QUEUE_PROVIDER is %r — this worker only serves "
                  "queue mode. Set EASYCUSTOMS_QUEUE_PROVIDER=sqs (and "
                  "SQS_QUEUE_URL) in backend/.env.", settings.queue_provider)
        return 2
    if settings.database_url.startswith("sqlite"):
        log.warning("DATABASE is SQLite (%s): correct ONLY when this worker and "
                    "the API share one machine and one file. On separate "
                    "machines this worker would extract into a database the "
                    "API never reads — use Postgres.", settings.database_url)
    init_db()
    sqs = make_sqs_client(settings)
    _install_signal_handlers()
    log.info("worker up: queue=%s concurrency=%s heartbeat=%ss/extend=%ss",
             settings.sqs_queue_url, settings.worker_concurrency,
             settings.sqs_heartbeat_seconds, settings.sqs_visibility_extend_seconds)
    inflight: set = set()
    with ThreadPoolExecutor(max_workers=settings.worker_concurrency,
                            thread_name_prefix="extract") as pool:
        while not _stop.is_set():
            inflight = {f for f in inflight if not f.done()}
            free = settings.worker_concurrency - len(inflight)
            if free <= 0:
                wait(inflight, timeout=5, return_when=FIRST_COMPLETED)
                continue
            try:
                resp = sqs.receive_message(
                    QueueUrl=settings.sqs_queue_url,
                    MaxNumberOfMessages=min(10, free),
                    WaitTimeSeconds=20,                    # long poll
                    # Named here so the head of every run is covered no
                    # matter what default the queue was created with — the
                    # heartbeat's first beat lands well inside this.
                    VisibilityTimeout=settings.sqs_visibility_extend_seconds,
                    AttributeNames=["ApproximateReceiveCount"])
            except Exception as e:
                log.error("receive_message failed (%s): %s — backing off 10s",
                          type(e).__name__, e)
                _stop.wait(10)
                continue
            for m in resp.get("Messages", []):
                inflight.add(pool.submit(process_message, sqs, settings, m))
        n = sum(1 for f in inflight if not f.done())
        if n:
            log.info("draining %s in-flight extraction(s)…", n)
        # ThreadPoolExecutor.__exit__ waits for them.
    log.info("worker stopped cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
