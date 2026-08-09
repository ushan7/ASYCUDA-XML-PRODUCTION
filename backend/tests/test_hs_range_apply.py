"""Bulk HS apply — one DB-validated code stamped on a printer-style SN range
("1-15, 19, 80" or "all") from the Detailed Review.  Additional facility on
top of the per-row hs-review channel: same write-time validation, same
recompute path, and a later per-row pick still overrides a single row."""
import pytest

from fastapi.testclient import TestClient

from app.database import SessionLocal, init_db
from app.main import app
from app.models import AuditEvent, Job
from app.review.item_mutations import ItemMutationError, parse_sn_ranges

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


def _hs_range(client, job_id, code, sn_range, source="detailed_review_hs_search"):
    return client.post(f"/api/jobs/{job_id}/items/hs-review-range", json={
        "final_hs_code": code, "sn_range": sn_range, "hs_review_source": source})


# --------------------------------------------------------------------------- #
# Printer-style range parser
# --------------------------------------------------------------------------- #
def test_parse_sn_ranges_valid():
    assert parse_sn_ranges("1-15, 19, 80", 100) == list(range(1, 16)) + [19, 80]
    assert parse_sn_ranges("3", 5) == [3]
    assert parse_sn_ranges(" 2 - 4 ", 5) == [2, 3, 4]
    assert parse_sn_ranges("4,2,2-3", 5) == [2, 3, 4]          # dedup + sort
    assert parse_sn_ranges("1–3", 5) == [1, 2, 3]              # en-dash tolerated
    assert parse_sn_ranges("all", 4) == [1, 2, 3, 4]
    assert parse_sn_ranges("ALL", 2) == [1, 2]
    assert parse_sn_ranges("1,,3", 5) == [1, 3]                # empty token skipped


def test_parse_sn_ranges_rejects():
    for spec, code in [("", "SN_RANGE_EMPTY"), ("  ", "SN_RANGE_EMPTY"),
                       (",", "SN_RANGE_EMPTY"),
                       ("5-2", "SN_RANGE_INVALID"), ("a-b", "SN_RANGE_INVALID"),
                       ("1-2-3", "SN_RANGE_INVALID"), ("1.5", "SN_RANGE_INVALID"),
                       ("0", "SN_RANGE_OUT_OF_BOUNDS"), ("1-99", "SN_RANGE_OUT_OF_BOUNDS"),
                       ("7", "SN_RANGE_OUT_OF_BOUNDS")]:
        with pytest.raises(ItemMutationError) as exc:
            parse_sn_ranges(spec, 6)
        assert exc.value.code == code, spec
        assert exc.value.status_code == 422


# --------------------------------------------------------------------------- #
# Endpoint behaviour
# --------------------------------------------------------------------------- #
def test_bulk_hs_applied_to_selected_rows_only(client):
    job_id, review = _reviewed_job(client)
    rows = review["item_details"]
    assert len(rows) >= 3
    spec = f"1-2, {len(rows)}"
    resp = _hs_range(client, job_id, "01012100000", spec)
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied_sns"] == [1, 2, len(rows)]
    assert body["applied_count"] == 3
    fresh = body["critical_review"]["item_details"]
    for row in fresh:
        if row["sn"] in (1, 2, len(rows)):
            assert row["final_hs"] == "01012100000"
            assert row["hs_explicit"] is True and row["hs_confidence"] == "1.00"
            assert row["hs_source"] == "DETAILED_REVIEW_HS_SEARCH"
        else:
            assert row["final_hs"] != "01012100000"
    db = SessionLocal()
    try:
        ev = db.query(AuditEvent).filter_by(job_id=job_id,
                                            event_code="ITEM_HS_REVIEWED_RANGE").one()
        assert ev.payload["sns"] == [1, 2, len(rows)]
        assert ev.payload["final_hs_code"] == "01012100000"
    finally:
        db.close()


def test_bulk_hs_all_rows_and_per_row_override(client):
    job_id, review = _reviewed_job(client)
    rows = review["item_details"]
    resp = _hs_range(client, job_id, "01012100000", "all")
    assert resp.status_code == 200
    assert resp.json()["applied_count"] == len(rows)
    assert all(r["final_hs"] == "01012100000"
               for r in resp.json()["critical_review"]["item_details"])
    # a later per-row pick overrides just that one row
    target = rows[1]
    r = client.post(f"/api/jobs/{job_id}/items/hs-review", json={
        "item_id": target["item_id"], "final_hs_code": "85044090900",
        "hs_review_source": "detailed_review_hs_search"})
    assert r.status_code == 200
    fresh = r.json()["critical_review"]["item_details"]
    for row in fresh:
        want = "85044090900" if row["item_id"] == target["item_id"] else "01012100000"
        assert row["final_hs"] == want


def test_bulk_hs_rejections_store_nothing(client):
    job_id, review = _reviewed_job(client)
    n = len(review["item_details"])
    r = _hs_range(client, job_id, "99999999999", "all")
    assert r.status_code == 422 and r.json()["code"] == "HS_NOT_IN_DATABASE"
    r = _hs_range(client, job_id, "8504", "all")
    assert r.status_code == 422 and r.json()["code"] == "HS_NOT_11_DIGITS"
    r = _hs_range(client, job_id, "01012100000", "all", source="llm_suggestion")
    assert r.status_code == 422 and r.json()["code"] == "HS_REVIEW_SOURCE_INVALID"
    r = _hs_range(client, job_id, "01012100000", f"1-{n + 5}")
    assert r.status_code == 422 and r.json()["code"] == "SN_RANGE_OUT_OF_BOUNDS"
    r = _hs_range(client, job_id, "01012100000", "banana")
    assert r.status_code == 422 and r.json()["code"] == "SN_RANGE_INVALID"
    r = _hs_range(client, job_id, "01012100000", "")
    assert r.status_code == 422 and r.json()["code"] == "SN_RANGE_EMPTY"
    db = SessionLocal()
    try:
        assert not (db.get(Job, job_id).item_mutations or {}).get("hs_selections")
    finally:
        db.close()


def test_bulk_hs_invalidates_xml(client):
    job_id, review = _reviewed_job(client)
    fin = client.post(f"/api/jobs/{job_id}/finalize",
                      json=dict(FINALIZE_BODY,
                                review_fingerprint=review["review_fingerprint"]))
    assert fin.status_code == 200 and fin.json()["ready_for_xml"] is True
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 200
    # invalid submission leaves the XML alone
    assert _hs_range(client, job_id, "99999999999", "all").status_code == 422
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 200
    # valid bulk apply invalidates it
    assert _hs_range(client, job_id, "01012100000", "1").status_code == 200
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 404


def test_bulk_hs_selection_follows_item_id_after_reorder(client):
    job_id, review = _reviewed_job(client)
    rows = review["item_details"]
    target = rows[2]
    assert _hs_range(client, job_id, "01012100000", "3").status_code == 200
    # deleting row 1 shifts SNs; the stamped selection follows the item_id
    assert client.request("DELETE", f"/api/jobs/{job_id}/items/{rows[0]['item_id']}",
                          json={"confirmation_sn": "1"}).status_code == 200
    fresh = client.get(f"/api/jobs/{job_id}/critical-review").json()
    moved = next(x for x in fresh["item_details"] if x["item_id"] == target["item_id"])
    assert moved["sn"] == 2 and moved["final_hs"] == "01012100000"
