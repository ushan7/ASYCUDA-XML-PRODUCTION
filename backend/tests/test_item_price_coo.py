"""Item price = printed line total (never qty x unit), and the bulk
"apply COO to all items" reviewer action (user rules 2026-07-18)."""
import json
from decimal import Decimal

import pytest

from fastapi.testclient import TestClient

from app.config import SAMPLE_DIR
from app.database import SessionLocal, init_db
from app.main import app
from app.models import AuditEvent, Job


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


def _custom_invoice_job(client, rows, header_overrides=None):
    job_id = client.post("/api/jobs").json()["job_id"]
    fx = json.loads((SAMPLE_DIR / "fixtures" / "invoice.json").read_text())
    fx["rows"] = rows
    fx["totals"] = None
    if header_overrides:
        fx["header"].update(header_overrides)
    pdf = (SAMPLE_DIR / "sample_invoice.pdf").read_bytes()
    up = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                     files={"file": ("inv.pdf", pdf, "application/pdf")},
                     data={"fixture": json.dumps(fx)})
    assert up.status_code == 200
    return job_id, client.get(f"/api/jobs/{job_id}/critical-review").json()


def _row(**kw):
    base = {"source_page_no": 1, "source_row_index": 1, "line_no_raw": "1",
            "description_raw": "Widget", "quantity_raw": "4", "uom_raw": "PCS",
            "unit_price_raw": None, "line_total_raw": "123.45", "currency_raw": "USD",
            "hs_code_raw": "85044090900", "country_of_origin_raw": "CN",
            "item_weight_scope": "UNKNOWN", "row_classification": "REAL_GOODS_ITEM"}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# Item price = printed line total
# --------------------------------------------------------------------------- #
def test_price_is_printed_total_when_unit_price_missing(client):
    _, review = _custom_invoice_job(client, [_row(unit_price_raw="", line_total_raw="123.45")])
    row = review["item_details"][0]
    assert row["total_price"] == "123.45"              # printed total, NOT 4 x 0 = 0
    assert review["calculated_goods_total"] == "123.45"


def test_printed_total_wins_over_qty_times_unit(client):
    # qty x unit = 4 x 10 = 40, but the printed total is 55.00 -> total wins
    _, review = _custom_invoice_job(client, [
        _row(quantity_raw="4", unit_price_raw="10.00", line_total_raw="55.00")])
    assert review["item_details"][0]["total_price"] == "55.00"


def test_missing_total_estimated_with_warning(client):
    _, review = _custom_invoice_job(client, [
        _row(quantity_raw="3", unit_price_raw="7.00", line_total_raw="")])
    assert review["item_details"][0]["total_price"] == "21.00"   # last-resort estimate
    assert any(w["code"] == "ITEM_TOTAL_ESTIMATED" for w in review["warnings"])


# --------------------------------------------------------------------------- #
# COO: exporter fallback + bulk apply-to-all
# --------------------------------------------------------------------------- #
def test_missing_item_coo_falls_back_to_exporter_country(client):
    _, review = _custom_invoice_job(
        client, [_row(country_of_origin_raw="")],
        header_overrides={"exporter": {"name_raw": "Acme", "country_raw": "China"}})
    row = review["item_details"][0]
    assert row["coo"] == "CN"                          # exporter country used


def _apply_coo(client, job_id, coo):
    return client.post(f"/api/jobs/{job_id}/items/coo-all",
                       json={"country_of_origin": coo})


def test_apply_coo_to_all_items(client):
    job_id, review = _reviewed_job(client)
    assert any(r["coo"] != "JP" for r in review["item_details"])   # demo is CN
    r = _apply_coo(client, job_id, "Japan")
    assert r.status_code == 200
    cr = r.json()["critical_review"]
    assert all(row["coo"] == "JP" for row in cr["item_details"])   # every item
    db = SessionLocal()
    try:
        assert db.get(Job, job_id).item_mutations["coo_all"] == "JP"
        assert db.query(AuditEvent).filter_by(
            job_id=job_id, event_code="ITEM_COO_APPLIED_ALL").count() == 1
    finally:
        db.close()


def test_apply_coo_survives_recompute_and_invalidates_xml(client):
    job_id, review = _reviewed_job(client)
    fin = client.post(f"/api/jobs/{job_id}/finalize", json={
        "manual_insurance_amount": "1665.49", "exchange_rate": "145.76",
        "field_40_confirmed": True,
    "border_mode": "01", "inland_mode_of_transport": "09",
        "review_fingerprint": review["review_fingerprint"]})
    assert fin.status_code == 200
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 200
    assert _apply_coo(client, job_id, "US").status_code == 200
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 404   # stale XML gone
    fresh = client.get(f"/api/jobs/{job_id}/critical-review").json()
    assert all(row["coo"] == "US" for row in fresh["item_details"])   # durable


def test_per_item_coo_edit_overrides_apply_all(client):
    job_id, review = _reviewed_job(client)
    assert _apply_coo(client, job_id, "Japan").status_code == 200
    target = review["item_details"][1]
    r = client.patch(f"/api/jobs/{job_id}/items/{target['item_id']}",
                     json={"fields": {"country_of_origin": "Germany"}})
    assert r.status_code == 200
    rows = r.json()["critical_review"]["item_details"]
    edited = next(x for x in rows if x["item_id"] == target["item_id"])
    assert edited["coo"] == "DE"                       # per-item wins
    assert all(x["coo"] == "JP" for x in rows if x["item_id"] != target["item_id"])


def test_apply_all_clears_prior_per_item_coo_edit(client):
    job_id, review = _reviewed_job(client)
    target = review["item_details"][0]
    assert client.patch(f"/api/jobs/{job_id}/items/{target['item_id']}",
                        json={"fields": {"country_of_origin": "Germany"}}).status_code == 200
    r = _apply_coo(client, job_id, "Japan")
    assert r.status_code == 200
    rows = r.json()["critical_review"]["item_details"]
    assert all(x["coo"] == "JP" for x in rows)         # the DE edit was cleared


def test_invalid_coo_rejected(client):
    job_id, _ = _reviewed_job(client)
    r = _apply_coo(client, job_id, "Wakanda")
    assert r.status_code == 422 and r.json()["code"] == "COO_INVALID"
    db = SessionLocal()
    try:
        assert not (db.get(Job, job_id).item_mutations or {}).get("coo_all")
    finally:
        db.close()
