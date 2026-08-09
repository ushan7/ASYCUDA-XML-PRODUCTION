"""Extracting a document after finalize invalidates what came before (W8).

Every reviewer mutation (add / delete / HS / COO / shipment totals / field
edit) calls _invalidate_derived. Extraction did not — it took no job lock and
cleared nothing — so a document extracted after finalize left the stored
review, declaration and XML artifact in place, and GET /jobs/{id}/xml kept
answering 200 with the superseded file.

The scenario is mundane, which is what makes it dangerous: finalize, notice a
document was wrong, upload the corrected one, extract, download. You get the
old XML. Unlike a doubled goods total there is no number to question — it just
describes a shipment you no longer have.

Also pins REVIEW_STALE at 409. It and ITEM_COUNT_MISMATCH mean the same thing
to a caller ("not finalized, go re-review"); REVIEW_STALE used to answer 200.
"""
import json

import pytest

from fastapi.testclient import TestClient

from app.config import SAMPLE_DIR
from app.database import SessionLocal, init_db
from app.demo import seed_demo_job
from app.domain.enums import DeclaredRole
from app.main import app
from app.models import AuditEvent
from app import services

FINALIZE_BODY = {
    "manual_insurance_amount": "1665.49", "exchange_rate": "145.76",
    "manifest_no": "2026/1436", "field_18_transport_identity": "BA16CHA8099",
    "field_21_transport_identity": "BA16CHA8099", "field_40_confirmed": True,
    "border_mode": "01", "inland_mode_of_transport": "09",
}


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def _packing_fixture():
    return json.loads((SAMPLE_DIR / "fixtures" / "packing_list.json").read_text())


def _corrected_packing_bytes():
    """A re-issued packing list: same shipment, different file."""
    pdf = SAMPLE_DIR / "sample_packing_list.pdf"
    base = pdf.read_bytes() if pdf.exists() else b"%PDF-1.4\n"
    return base + b"\n% corrected\n"


def _finalized_job(db):
    job = seed_demo_job(db)
    db.commit()
    review = services.critical_review(db, job)
    db.commit()
    services.finalize_job(db, job, dict(FINALIZE_BODY,
                                        review_fingerprint=review["review_fingerprint"]))
    db.commit()
    return job


# --------------------------------------------------------------------------- #
# Invalidation
# --------------------------------------------------------------------------- #
def test_extraction_after_finalize_discards_the_superseded_xml():
    init_db()
    db = SessionLocal()
    job = _finalized_job(db)
    assert services.latest_xml(db, job.id) is not None      # the old XML exists
    assert job.declaration is not None
    assert job.critical_review is not None

    # the reviewer spots a bad packing list and attaches the corrected one
    services.add_document(db, job, DeclaredRole.PACKING_LIST, "packing_list_corrected.pdf",
                          _corrected_packing_bytes(), _packing_fixture())
    db.commit()

    assert services.latest_xml(db, job.id) is None          # not served any more
    assert job.declaration is None
    assert job.critical_review is None
    db.close()


def test_the_invalidation_is_audited():
    init_db()
    db = SessionLocal()
    job = _finalized_job(db)
    services.add_document(db, job, DeclaredRole.PACKING_LIST, "packing_list_corrected.pdf",
                          _corrected_packing_bytes(), _packing_fixture())
    db.commit()
    events = [e.event_code for e in db.query(AuditEvent).filter(AuditEvent.job_id == job.id)]
    assert "DERIVED_STATE_INVALIDATED" in events
    db.close()


def test_xml_endpoint_404s_instead_of_serving_the_stale_file(client):
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    review = client.get(f"/api/jobs/{job_id}/critical-review").json()
    client.post(f"/api/jobs/{job_id}/finalize",
                json=dict(FINALIZE_BODY, review_fingerprint=review["review_fingerprint"]))
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 200

    r = client.post(f"/api/jobs/{job_id}/documents/PACKING_LIST",
                    files={"file": ("packing_list_corrected.pdf", _corrected_packing_bytes(),
                                    "application/pdf")},
                    data={"fixture": json.dumps(_packing_fixture())})
    assert r.status_code == 200
    # the superseded XML is gone rather than downloadable
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/declaration").status_code == 404


def test_a_rebuilt_declaration_is_downloadable_again(client):
    """Invalidation must not strand the job: re-review and re-finalize work."""
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    review = client.get(f"/api/jobs/{job_id}/critical-review").json()
    client.post(f"/api/jobs/{job_id}/finalize",
                json=dict(FINALIZE_BODY, review_fingerprint=review["review_fingerprint"]))
    client.post(f"/api/jobs/{job_id}/documents/PACKING_LIST",
                files={"file": ("pl2.pdf", _corrected_packing_bytes(), "application/pdf")},
                data={"fixture": json.dumps(_packing_fixture())})
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 404

    fresh = client.get(f"/api/jobs/{job_id}/critical-review").json()
    client.post(f"/api/jobs/{job_id}/finalize",
                json=dict(FINALIZE_BODY, review_fingerprint=fresh["review_fingerprint"]))
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 200


# --------------------------------------------------------------------------- #
# ...without disturbing the ordinary path
# --------------------------------------------------------------------------- #
def test_extraction_before_any_review_invalidates_nothing():
    """The demo seeds four documents back to back. Nothing derived exists yet,
    so invalidation is a no-op and no spurious audit event is written."""
    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)
    db.commit()
    codes = [e.event_code for e in db.query(AuditEvent).filter(AuditEvent.job_id == job.id)]
    assert "DERIVED_STATE_INVALIDATED" not in codes
    review = services.critical_review(db, job)
    db.commit()
    assert review["invoice_item_count"] == 119               # unchanged end to end
    db.close()


def test_review_then_extract_then_review_recomputes_cleanly():
    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)
    db.commit()
    services.critical_review(db, job)
    db.commit()
    services.add_document(db, job, DeclaredRole.PACKING_LIST, "pl2.pdf",
                          _corrected_packing_bytes(), _packing_fixture())
    db.commit()
    assert job.critical_review is None
    again = services.critical_review(db, job)                # recomputes on demand
    db.commit()
    assert again["invoice_item_count"] == 119
    db.close()


# --------------------------------------------------------------------------- #
# REVIEW_STALE is a refusal, and says so
# --------------------------------------------------------------------------- #
def test_review_stale_returns_409_like_item_count_mismatch(client):
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    client.get(f"/api/jobs/{job_id}/critical-review")
    r = client.post(f"/api/jobs/{job_id}/finalize",
                    json=dict(FINALIZE_BODY, review_fingerprint="not-the-current-fingerprint"))
    assert r.status_code == 409
    assert r.json()["status"] == "REVIEW_STALE"
    # nothing was built from the stale review
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 404


def test_a_good_finalize_still_returns_200(client):
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    review = client.get(f"/api/jobs/{job_id}/critical-review").json()
    r = client.post(f"/api/jobs/{job_id}/finalize",
                    json=dict(FINALIZE_BODY, review_fingerprint=review["review_fingerprint"]))
    assert r.status_code == 200
    assert r.json()["ready_for_xml"] is True
