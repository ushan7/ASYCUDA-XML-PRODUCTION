"""The upload -> extract step must keep what it computes, once, and say so.

Three defects, all on the live `POST .../documents/{doc_id}/extract` path and
all invisible to the suite because no test drove that endpoint (every other
test seeds documents through the fixture path, which never loads
``job.documents`` and therefore never triggered the first one):

  1. THE RESULT WAS THROWN AWAY.  ``run_extraction`` set status,
     raw_extraction, role_match and warnings on the document, then took the job
     lock, where ``_fresh_under_lock`` refreshes the Job.  ``Job.documents``
     cascades "all" -- which includes refresh-expire -- and the session runs
     with autoflush=False, so that refresh expired the document and discarded
     every un-flushed change.  The commit that followed wrote the job status
     and a DOCUMENT_EXTRACTED audit event and nothing else: the trail claimed
     success while the row still read UPLOADED.  The reviewer saw the box go
     back to "uploaded -- pending" and paid for the same extraction again.
     Observed in the field: three full OCR+LLM rounds on one document, three
     DOCUMENT_EXTRACTED events, raw_extraction still NULL.

  2. NOTHING CLAIMED THE DOCUMENT.  The endpoint's status check reads outside
     any transaction, so a double-submit or a second tab each passed it and
     started their own paid round on the same file, the slower one overwriting
     the faster one's rows.

  3. IN-FLIGHT WORK WAS INVISIBLE.  A document being extracted was
     indistinguishable in the DB from one merely waiting, so a reload during
     extraction showed "uploaded -- pending" (inviting defect 2), a critical
     review could be computed as if the document did not exist, and a document
     left claimed by a killed process would have been stuck forever -- hence
     the startup recovery.
"""
import json

import pytest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import services
from app.config import SAMPLE_DIR
from app.database import SessionLocal, init_db
from app.demo import seed_demo_job
from app.domain.enums import DeclaredRole, DocumentStatus
from app.domain.errors import BlockingValidationError
from app.extraction.service import extract_document as real_extract_document
from app.main import app
from app.models import AuditEvent, Document, Job


def _events(db, job_id) -> list[str]:
    return [e.event_code for e in
            db.scalars(select(AuditEvent).where(AuditEvent.job_id == job_id))]


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def _invoice_fixture() -> dict:
    return json.loads((SAMPLE_DIR / "fixtures" / "invoice.json").read_text())


def _offline_extract(monkeypatch, *, before=None):
    """Make the live extract path deterministic: the offline extractor, driven
    by the demo invoice fixture, instead of Mistral OCR + an LLM.  Everything
    around it -- endpoint, claim, session handling, persistence -- stays real,
    which is the part under test.  ``before`` runs inside the extraction, i.e.
    while the claim is held."""
    fixture = _invoice_fixture()

    def _fake(role, ocr, fx, deadline=None):
        if before is not None:
            before()
        return real_extract_document(role, ocr, fixture)

    monkeypatch.setattr(services, "extract_document", _fake)


def _job_with_an_unextracted_invoice(db):
    """A job whose INVOICE is uploaded, OCR'd and waiting for extraction --
    the state the Continue button acts on.  Seeded from the demo (so the OCR
    envelope is genuine) and wound back."""
    job = seed_demo_job(db)
    doc = next(d for d in job.documents if d.declared_role == DeclaredRole.INVOICE.value)
    doc.status = DocumentStatus.UPLOADED.value
    doc.raw_extraction = None
    doc.role_match = None
    doc.warnings = []
    db.commit()
    return job, doc


# --------------------------------------------------------------------------- #
# 1 · the result survives the job lock
# --------------------------------------------------------------------------- #
def test_extract_endpoint_persists_the_extraction(client, monkeypatch):
    """The regression: extract through the real endpoint, then read the row
    back from a DIFFERENT session.  Before the fix this returned UPLOADED with
    a NULL raw_extraction while the response and the audit trail both said
    the extraction had succeeded."""
    db = SessionLocal()
    job, doc = _job_with_an_unextracted_invoice(db)
    job_id, doc_id = job.id, doc.id
    db.close()

    _offline_extract(monkeypatch)
    r = client.post(f"/api/jobs/{job_id}/documents/{doc_id}/extract")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == DocumentStatus.EXTRACTED.value

    check = SessionLocal()
    stored = check.get(Document, doc_id)
    assert stored.status == DocumentStatus.EXTRACTED.value      # not UPLOADED
    assert stored.raw_extraction, "the extracted payload was discarded"
    assert stored.raw_extraction["rows"], "rows were extracted but not stored"
    assert stored.role_match is True
    check.close()


def test_the_audit_trail_cannot_claim_more_than_the_row_holds(client, monkeypatch):
    """A DOCUMENT_EXTRACTED event and an unextracted document is the exact
    contradiction the field failure produced.  It must not be reachable."""
    db = SessionLocal()
    job, doc = _job_with_an_unextracted_invoice(db)
    job_id, doc_id = job.id, doc.id
    db.close()

    _offline_extract(monkeypatch)
    client.post(f"/api/jobs/{job_id}/documents/{doc_id}/extract")

    check = SessionLocal()
    stored = check.get(Document, doc_id)
    assert "DOCUMENT_EXTRACTED" in _events(check, job_id)
    assert stored.status == DocumentStatus.EXTRACTED.value
    assert stored.raw_extraction
    check.close()


def test_a_pending_document_change_survives_the_job_lock():
    """The mechanism itself, isolated: _fresh_under_lock must not be able to
    erase the caller's un-flushed writes.  Kept separate from the endpoint test
    because every job_lock site depends on this, not just extraction."""
    db = SessionLocal()
    job = seed_demo_job(db)
    db.commit()
    doc = next(d for d in job.documents if d.declared_role == DeclaredRole.INVOICE.value)
    doc_id = doc.id

    doc.status = DocumentStatus.ROLE_REJECTED.value       # pending, not flushed
    services._fresh_under_lock(db, job)                   # refresh cascades to job.documents
    db.commit()
    db.close()

    check = SessionLocal()
    assert check.get(Document, doc_id).status == DocumentStatus.ROLE_REJECTED.value
    check.close()


# --------------------------------------------------------------------------- #
# 2 · one extraction per document
# --------------------------------------------------------------------------- #
def test_a_second_extraction_is_refused_while_one_is_running(client):
    """Two runs on one document duplicate the OCR/LLM spend and the later
    result silently overwrites the earlier one."""
    db = SessionLocal()
    job, doc = _job_with_an_unextracted_invoice(db)
    doc.status = DocumentStatus.EXTRACTING.value      # a run is in flight
    db.commit()
    job_id, doc_id = job.id, doc.id
    db.close()

    r = client.post(f"/api/jobs/{job_id}/documents/{doc_id}/extract")
    assert r.status_code == 409
    assert "already running" in r.text


def test_the_claim_refuses_a_race_the_endpoint_check_lets_through(client):
    """The endpoint reads the status outside any transaction, so two requests
    can both pass it.  The status-guarded UPDATE is what actually decides."""
    db = SessionLocal()
    job, doc = _job_with_an_unextracted_invoice(db)
    doc.status = DocumentStatus.EXTRACTING.value
    db.commit()

    with pytest.raises(BlockingValidationError) as e:
        services.run_extraction(db, doc)               # the losing racer
    assert e.value.message.code == "EXTRACTION_ALREADY_RUNNING"
    db.close()


def test_the_claim_is_visible_to_everyone_else_while_it_runs(client, monkeypatch):
    """Committed before the slow work, not after: another request (another tab,
    the reviewer's reload) has to be able to see that an extraction is running
    -- otherwise the document reads as merely pending and invites a second."""
    db = SessionLocal()
    job, doc = _job_with_an_unextracted_invoice(db)
    job_id, doc_id = job.id, doc.id
    db.close()

    seen = {}

    def _look():
        other = SessionLocal()                          # a wholly separate session
        seen["status"] = other.get(Document, doc_id).status
        other.close()

    _offline_extract(monkeypatch, before=_look)
    client.post(f"/api/jobs/{job_id}/documents/{doc_id}/extract")

    assert seen["status"] == DocumentStatus.EXTRACTING.value


def test_the_review_refuses_while_a_document_is_still_extracting(client):
    """A running extraction carries no rows yet, so a review computed now
    would declare the shipment as if the document did not exist -- and would
    look perfectly healthy doing it."""
    db = SessionLocal()
    job, doc = _job_with_an_unextracted_invoice(db)
    doc.status = DocumentStatus.EXTRACTING.value
    db.commit()
    job_id = job.id
    db.close()

    r = client.get(f"/api/jobs/{job_id}/critical-review")
    assert r.status_code == 409
    assert r.json()["blocking_errors"][0]["code"] == "EXTRACTION_IN_PROGRESS"


# --------------------------------------------------------------------------- #
# 3 · a claim outlives the process that took it
# --------------------------------------------------------------------------- #
def test_an_interrupted_extraction_is_returned_to_the_queue():
    """`uvicorn --reload` restarts on every file edit.  Without recovery the
    claim it left behind is permanent and the document can never be extracted
    again -- "already running", with nothing running."""
    db = SessionLocal()
    job, doc = _job_with_an_unextracted_invoice(db)
    doc.status = DocumentStatus.EXTRACTING.value          # killed mid-run
    db.commit()
    doc_id, job_id = doc.id, job.id
    db.close()

    fresh = SessionLocal()
    recovered = services.recover_interrupted_extractions(fresh)
    assert doc_id in [d.id for d in recovered]
    fresh.close()

    check = SessionLocal()
    stored = check.get(Document, doc_id)
    assert stored.status == DocumentStatus.UPLOADED.value        # retryable again
    assert stored.ocr, "the paid OCR envelope must survive so the retry is free"
    assert any("EXTRACTION_INTERRUPTED" in w for w in stored.warnings)
    assert "DOCUMENT_EXTRACTION_INTERRUPTED" in _events(check, job_id)
    check.close()


def test_recovery_leaves_healthy_documents_alone():
    db = SessionLocal()
    job = seed_demo_job(db)
    db.commit()
    before = {d.id: d.status for d in job.documents}
    job_id = job.id
    db.close()

    fresh = SessionLocal()
    services.recover_interrupted_extractions(fresh)
    fresh.close()

    check = SessionLocal()
    after = {d.id: d.status for d in check.get(Job, job_id).documents}
    assert after == before
    check.close()
