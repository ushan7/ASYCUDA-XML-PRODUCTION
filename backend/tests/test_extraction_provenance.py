"""Supplied extraction values are gated on WHERE THEY CAME FROM, not on the
mere fact that some were supplied.

There is one thing that must never happen quietly: facts chosen outside this
server reaching a legally binding declaration looking like facts read off the
paper.  The first attempt to stop it refused every supplied extraction, which
also refused the bundled demo — the two travelled through one hook.  So
``POST /api/jobs/demo`` answered 409 on every deployment that had not set
EASYCUSTOMS_ALLOW_FIXTURE_UPLOADS, i.e. all of them, while the suite stayed
green because conftest.py sets that flag for the whole run.

The split is therefore tested with the flag FORCED OFF, which is the only
configuration that could have caught the original bug:

  * the demo (fixtures the server loads from its own sample_data) works, and
  * an upload that carries its own extraction (chosen by the caller) does not.

Plus the mark, which is what earns the demo its exemption: every seeded row
says so, the job says so, and the audit trail says so.
"""
import pytest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import SAMPLE_DIR, get_settings
from app.database import SessionLocal, init_db
from app.domain.enums import DeclaredRole, ExtractionProvenance
from app.domain.errors import BlockingValidationError
from app.main import app
from app.models import AuditEvent, Document


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fixtures_disabled(monkeypatch):
    """A default deployment: no supplied extraction values accepted.

    conftest turns the flag on for the whole run, so without this every test
    below would pass for the wrong reason — which is exactly how the demo
    stayed broken through a green suite.
    """
    monkeypatch.setattr(get_settings(), "allow_fixture_uploads", False, raising=False)
    assert get_settings().allow_fixture_uploads is False
    yield


@pytest.fixture(scope="module")
def invoice_bytes():
    pdf = SAMPLE_DIR / "sample_invoice.pdf"
    return pdf.read_bytes() if pdf.exists() else b"%PDF-1.4\nreal pdf\n"


# --------------------------------------------------------------------------- #
# The bug: the demo on a server that never opted into fixture uploads
# --------------------------------------------------------------------------- #
def test_demo_works_on_a_default_deployment(client, fixtures_disabled):
    r = client.post("/api/jobs/demo")
    assert r.status_code == 200, r.text          # was 409 FIXTURE_UPLOADS_DISABLED
    body = r.json()
    assert body["documents"] == 4

    job = client.get(f"/api/jobs/{body['job_id']}").json()
    assert {d["role"] for d in job["documents"]} == {
        "INVOICE", "PACKING_LIST", "AIR_WAYBILL", "BANKING"}
    # ...and it reached the far end: seeding that stops at UPLOADED is a demo
    # that shows an empty workspace.
    assert all(d["status"] == "EXTRACTED" for d in job["documents"])


def test_client_supplied_extraction_is_still_refused(client, fixtures_disabled, invoice_bytes):
    """The hazard the gate exists for is unchanged by the demo's exemption."""
    job_id = client.post("/api/jobs").json()["job_id"]
    r = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                    files={"file": ("invoice.pdf", invoice_bytes, "application/pdf")},
                    data={"fixture": '{"invoice_number": "MADE-UP-0001"}'})
    assert r.status_code == 403
    assert "EASYCUSTOMS_ALLOW_FIXTURE_UPLOADS" in r.json()["detail"]
    # nothing was stored: a refused upload must not leave a half-attached row
    assert client.get(f"/api/jobs/{job_id}").json()["documents"] == []


def test_ordinary_upload_is_unaffected(client, fixtures_disabled, invoice_bytes):
    """No fixture, no gate — and the row records OCR, because that is where its
    facts will come from."""
    job_id = client.post("/api/jobs").json()["job_id"]
    r = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                    files={"file": ("invoice.pdf", invoice_bytes, "application/pdf")})
    assert r.status_code == 200
    doc = client.get(f"/api/jobs/{job_id}").json()["documents"][0]
    assert doc["provenance"] == ExtractionProvenance.OCR.value


# --------------------------------------------------------------------------- #
# What the exemption is paid for with: the mark
# --------------------------------------------------------------------------- #
def test_demo_job_is_marked_everywhere_it_appears(client, fixtures_disabled):
    job_id = client.post("/api/jobs/demo").json()["job_id"]

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["is_demo"] is True
    assert all(d["provenance"] == ExtractionProvenance.BUNDLED_DEMO.value
               for d in job["documents"])

    listed = next(j for j in client.get("/api/jobs?limit=200").json()["jobs"]
                  if j["job_id"] == job_id)
    assert listed["is_demo"] is True


def test_real_job_is_not_marked_as_demo(client, fixtures_disabled, invoice_bytes):
    """The mark has to be able to be absent, or it says nothing."""
    job_id = client.post("/api/jobs").json()["job_id"]
    client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                files={"file": ("invoice.pdf", invoice_bytes, "application/pdf")})
    assert client.get(f"/api/jobs/{job_id}").json()["is_demo"] is False


def test_seeded_extraction_is_in_the_audit_trail(client, fixtures_disabled):
    """A post-clearance audit asks where a number came from months later, when
    nobody remembers which button was pressed."""
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    db = SessionLocal()
    try:
        events = db.scalars(select(AuditEvent).where(
            AuditEvent.job_id == job_id,
            AuditEvent.event_code == "EXTRACTION_SEEDED")).all()
        assert len(events) == 4                    # one per seeded document
        assert all(e.payload["provenance"] == ExtractionProvenance.BUNDLED_DEMO.value
                   for e in events)
        seeded = {e.payload["document_id"] for e in events}
        stored = set(db.scalars(select(Document.id).where(Document.job_id == job_id)))
        assert seeded == stored
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# A forgotten argument must fail CLOSED
# --------------------------------------------------------------------------- #
def test_supplied_extraction_without_a_declared_provenance_is_treated_as_client_supplied(
        client, fixtures_disabled, invoice_bytes, monkeypatch):
    """``provenance`` defaults to OCR — the value that means "read from the
    document".  A caller that supplies facts and forgets to say where they came
    from must not inherit that claim, or the column launders exactly what it
    was added to expose.  The fallback is the gated one, so the mistake shows
    up as a refusal instead of as a plausible-looking row.
    """
    from app import services

    job_id = client.post("/api/jobs").json()["job_id"]
    db = SessionLocal()
    try:
        job = services.get_job(db, job_id, principal=services.SYSTEM_PRINCIPAL)
        with pytest.raises(BlockingValidationError) as e:
            services.add_document(db, job, DeclaredRole.INVOICE, "invoice.pdf",
                                  invoice_bytes, {"invoice_number": "MADE-UP"})
        assert e.value.message.code == "FIXTURE_UPLOADS_DISABLED"
    finally:
        db.rollback()
        db.close()
