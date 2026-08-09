"""Editable Field 9 (Traders/Financial/Financial_name).

Default: recomposed deterministically from the reviewed values.  When the
reviewer overrides it, the exact text is used verbatim in the XML — an
accidental blank falls back to recomposition so document references are never
silently dropped."""
import pytest
from lxml import etree

from fastapi.testclient import TestClient

from app.database import init_db
from app.main import app

FINALIZE_BODY = {
    "manual_insurance_amount": "1665.49", "exchange_rate": "145.76",
    "field_40_confirmed": True,
    "border_mode": "01", "inland_mode_of_transport": "09",
}


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def _reviewed_job(client):
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    r = client.get(f"/api/jobs/{job_id}/critical-review")
    assert r.status_code == 200
    return job_id, r.json()


def _financial_name(client, job_id):
    root = etree.fromstring(client.get(f"/api/jobs/{job_id}/xml").content)
    return root.findtext("Traders/Financial/Financial_name")


def test_field9_default_is_recomposed(client):
    job_id, review = _reviewed_job(client)
    preview = review["field_9_invoice_transport_document"]
    assert "MAWB NO:555-12345678 HAWB:DEMOHAWB0057" in preview   # demo default
    fin = client.post(f"/api/jobs/{job_id}/finalize",
                      json=dict(FINALIZE_BODY,
                                review_fingerprint=review["review_fingerprint"]))
    assert fin.status_code == 200 and fin.json()["ready_for_xml"] is True
    assert _financial_name(client, job_id) == preview          # recomposed == preview


def test_field9_override_used_verbatim(client):
    job_id, review = _reviewed_job(client)
    # Deliberately NOT the demo's MAWB — the point is that the override wins.
    custom = "MAWB NO:999-00000000 HAWB:CUSTOM01\nINVOICE NO:X DT: 01-JAN-2026\nLC NO:MYREF"
    fin = client.post(f"/api/jobs/{job_id}/finalize", json=dict(
        FINALIZE_BODY, review_fingerprint=review["review_fingerprint"],
        field_9_text=custom, field_9_override=True))
    assert fin.status_code == 200 and fin.json()["ready_for_xml"] is True
    got = _financial_name(client, job_id)
    assert got == custom                                       # multi-line preserved
    assert "555-12345678" not in got                          # auto value replaced


def test_field9_blank_override_falls_back_to_recomposition(client):
    job_id, review = _reviewed_job(client)
    fin = client.post(f"/api/jobs/{job_id}/finalize", json=dict(
        FINALIZE_BODY, review_fingerprint=review["review_fingerprint"],
        field_9_text="   ", field_9_override=True))
    assert fin.status_code == 200
    got = _financial_name(client, job_id)
    assert got == review["field_9_invoice_transport_document"]  # not blanked
    assert "MAWB NO:555-12345678" in got


def test_field9_override_flag_false_ignores_text(client):
    # text supplied but override not set -> recomposition still wins
    job_id, review = _reviewed_job(client)
    fin = client.post(f"/api/jobs/{job_id}/finalize", json=dict(
        FINALIZE_BODY, review_fingerprint=review["review_fingerprint"],
        field_9_text="SHOULD NOT APPEAR", field_9_override=False))
    assert fin.status_code == 200
    got = _financial_name(client, job_id)
    assert "SHOULD NOT APPEAR" not in got
    assert got == review["field_9_invoice_transport_document"]


def test_field9_override_audited(client):
    from app.database import SessionLocal
    from app.models import AuditEvent

    job_id, review = _reviewed_job(client)
    custom = "MAWB NO:111-22223333 HAWB:AUDITME"
    client.post(f"/api/jobs/{job_id}/finalize", json=dict(
        FINALIZE_BODY, review_fingerprint=review["review_fingerprint"],
        field_9_text=custom, field_9_override=True))
    db = SessionLocal()
    try:
        ev = db.query(AuditEvent).filter_by(
            job_id=job_id, event_code="CRITICAL_REVIEW_LOCKED").one()
        assert ev.payload["overrides"]["field_9_text"]["to"] == custom
    finally:
        db.close()


def test_field9_control_chars_do_not_crash_finalize(client):
    """Adversarial finding (major): a PDF paste can carry a form-feed/NUL; the
    override must not 500 — warn-mode always builds the XML."""
    job_id, review = _reviewed_job(client)
    custom = "MAWB NO:160-\x0cINVOICE\x00 NO:X HAWB:CTRL"   # form-feed + NUL interior
    fin = client.post(f"/api/jobs/{job_id}/finalize", json=dict(
        FINALIZE_BODY, review_fingerprint=review["review_fingerprint"],
        field_9_text=custom, field_9_override=True))
    assert fin.status_code == 200 and fin.json()["ready_for_xml"] is True
    got = _financial_name(client, job_id)
    assert "\x0c" not in got and "\x00" not in got         # stripped
    assert got == "MAWB NO:160-INVOICE NO:X HAWB:CTRL"      # rest verbatim


def test_field9_blank_override_not_audited(client):
    """Adversarial finding (minor): a blank-nullified override falls back to
    recomposition and must NOT be logged as a reviewer change."""
    from app.database import SessionLocal
    from app.models import AuditEvent

    job_id, review = _reviewed_job(client)
    client.post(f"/api/jobs/{job_id}/finalize", json=dict(
        FINALIZE_BODY, review_fingerprint=review["review_fingerprint"],
        field_9_text="   ", field_9_override=True))
    db = SessionLocal()
    try:
        ev = db.query(AuditEvent).filter_by(
            job_id=job_id, event_code="CRITICAL_REVIEW_LOCKED").one()
        assert "field_9_text" not in ev.payload["overrides"]
    finally:
        db.close()


def test_field9_default_recompose_includes_bill_of_lading(client):
    """Adversarial finding: the default recomposition adds the BL line — the
    client-side preview mirrors this so the gate display matches the XML."""
    job_id, review = _reviewed_job(client)
    fin = client.post(f"/api/jobs/{job_id}/finalize", json=dict(
        FINALIZE_BODY, review_fingerprint=review["review_fingerprint"],
        bill_of_lading_no="BL-TEST-123"))     # no override — pure recomposition
    assert fin.status_code == 200
    got = _financial_name(client, job_id)
    assert "B/L NO:BL-TEST-123" in got
    # the demo is an air shipment: the AWB line stays and the B/L is added under
    # it (a B/L only REPLACES the AWB line when there is no air waybill)
    assert "MAWB NO:555-12345678 HAWB:DEMOHAWB0057" in got
