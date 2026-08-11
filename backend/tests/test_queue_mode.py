"""Queue-mode extraction: the producer claim/enqueue contract and the worker's
message dispatch.

The invariants pinned here, and why each matters:

  * The producer claims BEFORE it sends, so a double-click cannot put two
    messages on the queue for one document — the claim is the deduplication.
  * A failed send releases the claim on the spot.  Without that the document
    is EXTRACTING forever: no message exists, no worker will release it, and
    queue mode disables the startup sweep that used to clear orphans.
  * A MESSAGE DIES WITH ITS ATTEMPT.  Success, skip, refusal and FAILURE all
    delete it; the retry for a failed extraction is the reviewer's Continue
    (a fresh claim, a fresh message), never an SQS redelivery.  FAILED is an
    invitation to press Continue, so auto-retrying here would race the human:
    two live messages for one document, two concurrent paid extractions, last
    writer silently winning.
  * The ONE exception is a well-formed message this build does not understand
    (a newer `v`/`kind` mid rolling upgrade): it is LEFT for an upgraded
    worker or the DLQ, because deleting it would strand its producer's claim
    as EXTRACTING with nothing left to release it.
  * So the only messages SQS ever re-delivers are those whose worker DIED
    mid-run — redelivery is the crash recovery, and the DLQ holds exactly the
    poison documents that kill workers.

No AWS anywhere: the SQS client is a recorder, the extraction a stub.
"""
import json
import threading

import pytest

from fastapi.testclient import TestClient

import worker
from app import queueing, services
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.domain.enums import DocumentStatus
from app.domain.errors import BlockingValidationError
from app.main import app
from app.models import AuditEvent, Document, Job

QUEUE_URL = "https://sqs.test.invalid/000000000000/pytest-queue"


class FakeSqs:
    """Records calls; optionally refuses sends like an unreachable queue."""

    def __init__(self, fail_send: bool = False):
        self.fail_send = fail_send
        self.sent: list[str] = []
        self.deleted: list[str] = []
        self.visibility: list[tuple[str, int]] = []
        # Set by the first heartbeat, so a test can block a stubbed extraction
        # until one has actually fired instead of sleeping and hoping.
        self.beat = threading.Event()

    def send_message(self, QueueUrl, MessageBody):
        assert QueueUrl == QUEUE_URL
        if self.fail_send:
            raise RuntimeError("simulated: sqs unreachable")
        self.sent.append(MessageBody)
        return {"MessageId": f"m-{len(self.sent)}"}

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)

    def change_message_visibility(self, QueueUrl, ReceiptHandle, VisibilityTimeout):
        self.visibility.append((ReceiptHandle, VisibilityTimeout))
        self.beat.set()


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def queue_mode(monkeypatch):
    """Flip the cached settings into queue mode with a recording client."""
    settings = get_settings()
    monkeypatch.setattr(settings, "queue_provider", "sqs")
    monkeypatch.setattr(settings, "sqs_queue_url", QUEUE_URL)
    fake = FakeSqs()
    monkeypatch.setattr(queueing, "_client", fake)
    yield fake
    queueing.reset_client_cache()


def _make_doc(status: str = DocumentStatus.UPLOADED.value) -> tuple[str, str]:
    """A job + document owned by the suite's operator, straight in the DB —
    the queue path never reads the file, so no upload is needed."""
    s = SessionLocal()
    try:
        job = Job(status="UPLOAD_COMPLETE", owner_key="pytest-operator")
        s.add(job)
        s.flush()
        doc = Document(job_id=job.id, declared_role="INVOICE",
                       original_file_name="queue-test.pdf", status=status)
        s.add(doc)
        s.commit()
        return job.id, doc.id
    finally:
        s.close()


def _doc_row(document_id: str) -> Document:
    s = SessionLocal()
    try:
        return s.get(Document, document_id)
    finally:
        s.close()


def _audit_codes(job_id: str) -> list[str]:
    s = SessionLocal()
    try:
        return [e.event_code for e in
                s.query(AuditEvent).filter(AuditEvent.job_id == job_id)]
    finally:
        s.close()


def _msg(payload, *, receipt="r-1", receive_count="1"):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return {"Body": body, "ReceiptHandle": receipt,
            "Attributes": {"ApproximateReceiveCount": receive_count}}


def _payload(job_id, document_id, **over):
    p = {"v": queueing.MESSAGE_V, "kind": queueing.KIND_EXTRACT_DOCUMENT,
         "job_id": job_id, "document_id": document_id, "actor": "pytest-operator"}
    p.update(over)
    return p


def _without_correlation(sent: dict) -> dict:
    """The message minus its request_id, which is a fresh value per call.

    Correlation only — the worker adopts it for logging and never decides
    anything with it — so the contract this compares is everything else.
    """
    assert sent.get("request_id"), (
        "the enqueuing request id must travel with the message, or the queued "
        "half of an extraction cannot be traced back to the upload")
    return {k: v for k, v in sent.items() if k != "request_id"}


# --------------------------------------------------------------------------- #
# Producer: POST /extract in queue mode
# --------------------------------------------------------------------------- #

def test_enqueue_claims_then_sends_and_reports_extracting(client, queue_mode):
    job_id, doc_id = _make_doc()
    r = client.post(f"/api/jobs/{job_id}/documents/{doc_id}/extract")
    assert r.status_code == 200
    body = r.json()
    # Not a failure shape, not a terminal shape: the SPA polls EXTRACTING.
    assert body["queued"] is True
    assert body["status"] == DocumentStatus.EXTRACTING.value
    assert body["role_match"] is None
    assert _doc_row(doc_id).status == DocumentStatus.EXTRACTING.value
    assert len(queue_mode.sent) == 1
    sent = json.loads(queue_mode.sent[0])
    assert _without_correlation(sent) == _payload(job_id, doc_id)
    assert "DOCUMENT_EXTRACTION_QUEUED" in _audit_codes(job_id)


def test_second_post_is_refused_not_requeued(client, queue_mode):
    job_id, doc_id = _make_doc()
    assert client.post(f"/api/jobs/{job_id}/documents/{doc_id}/extract").status_code == 200
    r = client.post(f"/api/jobs/{job_id}/documents/{doc_id}/extract")
    assert r.status_code == 409
    assert len(queue_mode.sent) == 1          # the claim deduplicates the queue


def test_failed_send_releases_the_claim(client, queue_mode):
    job_id, doc_id = _make_doc()
    queue_mode.fail_send = True
    r = client.post(f"/api/jobs/{job_id}/documents/{doc_id}/extract")
    assert r.status_code == 502
    assert r.json()["status"] == "FAILED"
    row = _doc_row(doc_id)
    assert row.status == DocumentStatus.UPLOADED.value      # back where it was
    assert any("QUEUE_SEND_FAILED" in w for w in (row.warnings or []))
    assert "DOCUMENT_EXTRACTION_QUEUE_FAILED" in _audit_codes(job_id)
    # …and the document is exactly as retriable as before the failed attempt.
    queue_mode.fail_send = False
    assert client.post(f"/api/jobs/{job_id}/documents/{doc_id}/extract").status_code == 200
    assert len(queue_mode.sent) == 1


def test_failed_send_from_FAILED_reverts_to_FAILED(client, queue_mode):
    job_id, doc_id = _make_doc(status=DocumentStatus.FAILED.value)
    queue_mode.fail_send = True
    assert client.post(f"/api/jobs/{job_id}/documents/{doc_id}/extract").status_code == 502
    assert _doc_row(doc_id).status == DocumentStatus.FAILED.value


def test_service_claim_refuses_taken_documents(queue_mode):
    _job_id, doc_id = _make_doc(status=DocumentStatus.EXTRACTING.value)
    s = SessionLocal()
    try:
        doc = s.get(Document, doc_id)
        with pytest.raises(BlockingValidationError):
            services.claim_for_queued_extraction(s, doc, actor="pytest-operator")
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Worker: one delivery, every branch
# --------------------------------------------------------------------------- #

@pytest.fixture()
def worker_env(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "queue_provider", "sqs")
    monkeypatch.setattr(settings, "sqs_queue_url", QUEUE_URL)
    # An hour-long first beat: no heartbeat fires inside any test.
    monkeypatch.setattr(settings, "sqs_heartbeat_seconds", 3600)
    return settings, FakeSqs()


def test_worker_runs_producer_claimed_document(worker_env, monkeypatch):
    settings, sqs = worker_env
    job_id, doc_id = _make_doc(status=DocumentStatus.EXTRACTING.value)
    seen = {}

    def fake_run(db, doc, fixture=None, *, actor, claim_from):
        seen["actor"], seen["claim_from"] = actor, claim_from
        doc.status = DocumentStatus.EXTRACTED.value
        db.commit()
        return doc

    monkeypatch.setattr(services, "run_extraction", fake_run)
    worker.process_message(sqs, settings, _msg(_payload(job_id, doc_id)))
    assert seen["actor"] == "pytest-operator"
    assert seen["claim_from"] == worker.CLAIM_FROM
    assert _doc_row(doc_id).status == DocumentStatus.EXTRACTED.value
    assert sqs.deleted == ["r-1"]             # success deletes the message


def test_worker_failure_deletes_the_message_and_leaves_the_retry_to_the_human(
        worker_env, monkeypatch):
    """A failed attempt takes its message with it.

    run_extraction has already persisted FAILED and the reason (pinned by
    test_real_claim_matches_an_EXTRACTING_row, which uses the real one), the
    SPA shows it, and Continue mints a NEW claim and a NEW message.  Keeping
    this message instead would put an SQS redelivery in a race with the human
    the FAILED status is inviting to press Continue: two live messages for one
    document and two concurrent paid extractions.
    """
    settings, sqs = worker_env
    job_id, doc_id = _make_doc(status=DocumentStatus.EXTRACTING.value)

    def fake_run(db, doc, fixture=None, *, actor, claim_from):
        raise RuntimeError("simulated LLM outage")

    monkeypatch.setattr(services, "run_extraction", fake_run)
    worker.process_message(sqs, settings,
                           _msg(_payload(job_id, doc_id), receive_count="2"))
    assert sqs.deleted == ["r-1"]
    # Nothing re-extended it either: the message is gone, not hidden.
    assert sqs.visibility == []


def test_worker_claim_refusal_deletes_message(worker_env, monkeypatch):
    settings, sqs = worker_env
    job_id, doc_id = _make_doc(status=DocumentStatus.EXTRACTING.value)

    def fake_run(db, doc, fixture=None, *, actor, claim_from):
        raise BlockingValidationError("EXTRACTION_ALREADY_RUNNING", "lost the race",
                                      scope="DOCUMENT", document_id=doc.id)

    monkeypatch.setattr(services, "run_extraction", fake_run)
    worker.process_message(sqs, settings, _msg(_payload(job_id, doc_id)))
    assert sqs.deleted == ["r-1"]


def test_worker_skips_terminal_and_released_documents(worker_env, monkeypatch):
    settings, sqs = worker_env

    def must_not_run(*a, **k):
        raise AssertionError("run_extraction must not be called")

    monkeypatch.setattr(services, "run_extraction", must_not_run)
    for status in (DocumentStatus.EXTRACTED.value,
                   DocumentStatus.ROLE_REVIEW_REQUIRED.value,
                   DocumentStatus.ROLE_REJECTED.value,
                   DocumentStatus.UPLOADED.value):    # released claim: reviewer decides
        job_id, doc_id = _make_doc(status=status)
        sqs.deleted.clear()
        worker.process_message(sqs, settings, _msg(_payload(job_id, doc_id)))
        assert sqs.deleted == ["r-1"], f"{status} must delete-and-skip"


def test_worker_does_not_run_FAILED_documents(worker_env, monkeypatch):
    """FAILED is deliberately NOT in CLAIM_FROM.

    A FAILED document with a live message means only that a delete was lost
    mid-crash — the attempt that failed already deleted its own.  Running here
    would race the reviewer's Continue, which mints its own claim and message.
    So the delivery is skipped and the stray message deleted.
    """
    settings, sqs = worker_env
    job_id, doc_id = _make_doc(status=DocumentStatus.FAILED.value)
    monkeypatch.setattr(
        services, "run_extraction",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("run_extraction must not be called for FAILED")))
    worker.process_message(sqs, settings,
                           _msg(_payload(job_id, doc_id), receive_count="2"))
    assert sqs.deleted == ["r-1"]
    assert _doc_row(doc_id).status == DocumentStatus.FAILED.value   # untouched


def test_worker_deletes_unknown_document_messages(worker_env):
    settings, sqs = worker_env
    worker.process_message(sqs, settings,
                           _msg(_payload("no-such-job", "no-such-doc")))
    assert sqs.deleted == ["r-1"]


def test_worker_deletes_mismatched_job_document_pair(worker_env):
    settings, sqs = worker_env
    _job_a, doc_a = _make_doc(status=DocumentStatus.EXTRACTING.value)
    job_b, _doc_b = _make_doc(status=DocumentStatus.EXTRACTING.value)
    worker.process_message(sqs, settings, _msg(_payload(job_b, doc_a)))
    assert sqs.deleted == ["r-1"]
    assert _doc_row(doc_a).status == DocumentStatus.EXTRACTING.value  # untouched


def test_worker_deletes_malformed_messages(worker_env):
    """Unintelligible bodies reference nothing recoverable: the log line is the
    forensic record, and the message goes rather than looping to the DLQ."""
    settings, sqs = worker_env
    for bad in ("not json",
                json.dumps(["list"]),
                json.dumps({"v": 1, "kind": queueing.KIND_EXTRACT_DOCUMENT,
                            "job_id": "", "document_id": "d"}),
                json.dumps({"v": 1, "kind": queueing.KIND_EXTRACT_DOCUMENT,
                            "job_id": "j", "document_id": 7})):
        sqs.deleted.clear()
        worker.process_message(sqs, settings, _msg(bad))
        assert sqs.deleted == ["r-1"], f"malformed body must be deleted: {bad!r}"


def test_worker_keeps_messages_it_does_not_understand(worker_env):
    """A WELL-FORMED message from a newer build (unknown `v` or `kind`) is left
    alone — mid rolling upgrade an old worker must not delete work it cannot
    do.  Deleting it would strand its producer's claim as EXTRACTING with no
    message left to release it; leaving it lets an upgraded worker, or
    eventually the DLQ, have it."""
    settings, sqs = worker_env
    for unsupported in (json.dumps({"v": 999, "kind": queueing.KIND_EXTRACT_DOCUMENT,
                                    "job_id": "j", "document_id": "d"}),
                        json.dumps({"v": 999}),
                        json.dumps({"v": 1, "kind": "some_future_kind",
                                    "job_id": "j", "document_id": "d"})):
        sqs.deleted.clear()
        worker.process_message(sqs, settings, _msg(unsupported))
        assert sqs.deleted == [], f"unsupported schema must be KEPT: {unsupported!r}"


def test_real_claim_matches_an_EXTRACTING_row():
    """The takeover claim (claim_from includes EXTRACTING) must MATCH a row
    whose status is already EXTRACTING — an UPDATE that sets a column to its
    current value still counts the row on SQLite and Postgres alike.  Pinned
    without stubs: if run_extraction gets PAST the claim it fails later (no
    stored file to OCR) and persists FAILED — whereas a claim miss raises
    BlockingValidationError and leaves the status untouched.  FAILED here
    proves the claim matched."""
    _job_id, doc_id = _make_doc(status=DocumentStatus.EXTRACTING.value)
    s = SessionLocal()
    try:
        doc = s.get(Document, doc_id)
        with pytest.raises(Exception) as exc:
            services.run_extraction(s, doc, actor="pytest-operator",
                                    claim_from=worker.CLAIM_FROM)
        assert not isinstance(exc.value, BlockingValidationError)
    finally:
        s.close()
    assert _doc_row(doc_id).status == DocumentStatus.FAILED.value


def test_heartbeat_extends_visibility_while_the_extraction_runs(worker_env, monkeypatch):
    """The in-flight message stays invisible for as long as the run takes.

    This is what makes redelivery mean "the worker DIED" and nothing else —
    the liveness signal the status column cannot carry.  Deterministic, not
    timed: the stubbed extraction blocks until a beat has actually landed.
    """
    settings, sqs = worker_env
    monkeypatch.setattr(settings, "sqs_heartbeat_seconds", 0.05)
    monkeypatch.setattr(settings, "sqs_visibility_extend_seconds", 600)
    job_id, doc_id = _make_doc(status=DocumentStatus.EXTRACTING.value)

    def slow_run(db, doc, fixture=None, *, actor, claim_from):
        assert sqs.beat.wait(10), "the heartbeat never extended the message"
        doc.status = DocumentStatus.EXTRACTED.value
        db.commit()
        return doc

    monkeypatch.setattr(services, "run_extraction", slow_run)
    worker.process_message(sqs, settings, _msg(_payload(job_id, doc_id)))
    assert sqs.visibility and all(v == ("r-1", 600) for v in sqs.visibility)
    assert sqs.deleted == ["r-1"]        # …and the delete still happens last


def test_parse_message_reports_state_and_payload():
    """('ok', payload) | ('malformed', None) | ('unsupported', None) — the
    three-way split is what lets process_message delete a malformed body but
    KEEP one it merely does not understand."""
    ok = worker.parse_message(json.dumps(
        {"v": queueing.MESSAGE_V, "kind": queueing.KIND_EXTRACT_DOCUMENT,
         "job_id": "j", "document_id": "d"}))
    # No actor on the wire: the worker names itself rather than inventing a human.
    # No request_id either — a message from an older producer still parses, and
    # the worker falls back to the document id for correlation.
    assert ok == ("ok", {"job_id": "j", "document_id": "d", "actor": "worker",
                         "request_id": ""})

    with_id = worker.parse_message(json.dumps(
        {"v": queueing.MESSAGE_V, "kind": queueing.KIND_EXTRACT_DOCUMENT,
         "job_id": "j", "document_id": "d", "request_id": "req-abc"}))
    assert with_id[1]["request_id"] == "req-abc"

    assert worker.parse_message("not json") == ("malformed", None)
    assert worker.parse_message(json.dumps(
        {"v": queueing.MESSAGE_V, "kind": queueing.KIND_EXTRACT_DOCUMENT,
         "job_id": "j"})) == ("malformed", None)
    assert worker.parse_message(json.dumps(
        {"v": 999, "kind": queueing.KIND_EXTRACT_DOCUMENT,
         "job_id": "j", "document_id": "d"})) == ("unsupported", None)
