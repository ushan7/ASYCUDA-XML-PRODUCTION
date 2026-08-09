"""Reviewer-corrected shipment totals as the FINAL allocation authority
(user rule 2026-07-18: after a correction, item CTN/weight sums MUST equal the
reviewer's values everywhere — review preview, declaration and XML)."""
from decimal import Decimal

import pytest

from fastapi.testclient import TestClient

from app.database import SessionLocal, init_db
from app.main import app
from app.models import AuditEvent, Job

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


def _set_totals(client, job_id, gross, packages, unit="KGM"):
    return client.post(f"/api/jobs/{job_id}/shipment-totals", json={
        "gross_weight": gross, "weight_unit": unit, "total_packages": packages})


def test_reviewer_totals_become_final_authority(client):
    job_id, review = _reviewed_job(client)
    assert review["gross_weight"] == "199.0000"          # extracted HAWB default
    r = _set_totals(client, job_id, "160", "9")
    assert r.status_code == 200
    cr = r.json()["critical_review"]
    assert cr["gross_weight"] == "160.0000" and cr["total_packages"] == "9.00"
    assert cr["gross_weight_source"] == "REVIEWER_OVERRIDE"
    assert any(w["code"] == "SHIPMENT_TOTALS_REVIEWED" for w in cr["warnings"])
    # the Detailed-Review allocation preview reconciles EXACTLY to the correction
    rows = cr["item_details"]
    assert sum(Decimal(x["gross"]) for x in rows) == Decimal("160")
    assert sum(Decimal(x["ctn"]) for x in rows) == Decimal("9")
    db = SessionLocal()
    try:
        assert db.query(AuditEvent).filter_by(
            job_id=job_id, event_code="SHIPMENT_TOTALS_REVIEWED").count() == 1
        assert db.get(Job, job_id).item_mutations["shipment_override"]["gross_weight"] == "160"
    finally:
        db.close()


def test_reviewer_totals_survive_recompute_and_flow_to_xml(client):
    job_id, _ = _reviewed_job(client)
    assert _set_totals(client, job_id, "160", "9").status_code == 200
    # durability: a fresh recompute (the step that previously wiped the
    # reviewer's correction) still shows the corrected authority
    fresh = client.get(f"/api/jobs/{job_id}/critical-review").json()
    assert fresh["gross_weight"] == "160.0000" and fresh["total_packages"] == "9.00"
    assert fresh["gross_weight_source"] == "REVIEWER_OVERRIDE"
    # finalize: declaration and XML reconcile to the reviewer's totals
    fin = client.post(f"/api/jobs/{job_id}/finalize",
                      json=dict(FINALIZE_BODY,
                                review_fingerprint=fresh["review_fingerprint"]))
    assert fin.status_code == 200
    decl = fin.json()
    assert decl["ready_for_xml"] is True
    assert decl["valuation"]["total_weight"] == "160.0000"
    assert sum(Decimal(i["gross_weight_kg"]) for i in decl["items"]) == Decimal("160")
    assert sum(Decimal(i["package_count"]) for i in decl["items"]) == Decimal("9")
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 200


def test_totals_change_invalidates_existing_xml(client):
    job_id, review = _reviewed_job(client)
    fin = client.post(f"/api/jobs/{job_id}/finalize",
                      json=dict(FINALIZE_BODY,
                                review_fingerprint=review["review_fingerprint"]))
    assert fin.status_code == 200
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 200
    assert _set_totals(client, job_id, "160", "9").status_code == 200
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 404   # stale XML gone


def test_confirmed_totals_reconcile_without_override(client):
    # direct path (no stored override): finalize-time confirmed values already
    # drive the exact-sum reconciliation — regression for the reported case
    job_id, review = _reviewed_job(client)
    fin = client.post(f"/api/jobs/{job_id}/finalize", json=dict(
        FINALIZE_BODY, review_fingerprint=review["review_fingerprint"],
        confirmed_gross_weight="160", confirmed_total_packages="9"))
    assert fin.status_code == 200
    decl = fin.json()
    assert sum(Decimal(i["gross_weight_kg"]) for i in decl["items"]) == Decimal("160")
    assert sum(Decimal(i["package_count"]) for i in decl["items"]) == Decimal("9")


def test_invalid_totals_rejected(client):
    job_id, _ = _reviewed_job(client)
    for gross, pkgs in (("0", "5"), ("-3", "5"), ("78", "0"), ("nan", "5"), ("abc", "5")):
        r = _set_totals(client, job_id, gross, pkgs)
        assert r.status_code == 422, (gross, pkgs)
    assert _set_totals(client, job_id, "78", "5", unit="XX").status_code == 422
    # nothing stored, review untouched
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        assert not (job.item_mutations or {}).get("shipment_override")
    finally:
        db.close()
