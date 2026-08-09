"""Reviewer add/delete item workflow — identity, ordering, tombstones, audit,
revision, XML invalidation and the mutation invariants (spec 2026-07-18).

Everything runs through the HTTP API against demo-fixture jobs (offline,
deterministic, no network)."""
import json
import os
from decimal import Decimal

import pytest

from fastapi.testclient import TestClient

from app.config import SAMPLE_DIR
from app.database import SessionLocal, init_db
from app.main import app
from app.models import AuditEvent, Job

DEMO_INVOICE_NO = "DEMO-209-1"
GOOD_SEED = {"description": "Reviewer test row", "quantity": 5, "uom": "PCS",
             "total_price": 100, "country_of_origin": "CN",
             "final_hs_code": "85044090900"}
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


def _reviewed_job(client) -> tuple[str, dict]:
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    r = client.get(f"/api/jobs/{job_id}/critical-review")
    assert r.status_code == 200
    return job_id, r.json()


def _rows(review: dict) -> list[dict]:
    return review["item_details"]


def _add(client, job_id, insertion_sn, *, invoice_id="", manual=True, item=None, **extra):
    body = {"insertion_sn": insertion_sn, "invoice_id": invoice_id,
            "manual_review_addition": manual, "item": item or {}, **extra}
    return client.post(f"/api/jobs/{job_id}/items", json=body)


def _delete(client, job_id, item_id, confirmation_sn):
    return client.request("DELETE", f"/api/jobs/{job_id}/items/{item_id}",
                          json={"confirmation_sn": str(confirmation_sn)})


def _overlay(job_id) -> dict:
    db = SessionLocal()
    try:
        return db.get(Job, job_id).item_mutations or {}
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Add
# --------------------------------------------------------------------------- #
def test_add_beginning_middle_end_exact_ordering(client):
    job_id, review = _reviewed_job(client)
    base_ids = [r["item_id"] for r in _rows(review)]
    n = len(base_ids)

    r1 = _add(client, job_id, 1).json()
    r2 = _add(client, job_id, 61).json()
    r3_resp = _add(client, job_id, n + 3)          # end of the now n+2 list
    r3 = r3_resp.json()
    assert r1["status"] == r2["status"] == r3["status"] == "ok"
    assert (r1["inserted_sn"], r2["inserted_sn"], r3["inserted_sn"]) == (1, 61, n + 3)

    rows = _rows(r3["critical_review"])
    ids = [r["item_id"] for r in rows]
    assert len(rows) == n + 3
    assert [r["sn"] for r in rows] == list(range(1, n + 4))       # resequenced
    # exact inserted ordering: new ids at 0 / 60 / end, sources otherwise intact
    assert ids[0] == r1["added_item_id"] and ids[60] == r2["added_item_id"]
    assert ids[-1] == r3["added_item_id"]
    assert [i for i in ids if i.startswith("src:")] == base_ids   # order preserved
    # ordered_item_ids is an exact duplicate-free permutation of the active rows
    ordered = _overlay(job_id)["ordered_item_ids"]
    assert ordered == ids and len(set(ordered)) == len(ordered)
    assert all(rows[k]["origin"] == "manual" for k in (0, 60, len(rows) - 1))


def test_add_insertion_sn_out_of_range(client):
    job_id, review = _reviewed_job(client)
    n = len(_rows(review))
    for bad in (0, -3, n + 2):
        r = _add(client, job_id, bad)
        assert r.status_code == 422
        assert r.json()["code"] == "INSERTION_SN_OUT_OF_RANGE"


def test_add_invoice_associated_binding(client):
    job_id, review = _reviewed_job(client)
    r = _add(client, job_id, 1, invoice_id=DEMO_INVOICE_NO, manual=False,
             item=GOOD_SEED).json()
    assert r["status"] == "ok"
    rec = _overlay(job_id)["manual_items"][0]
    inv = rec["invoice"]
    assert inv["invoice_no"] == DEMO_INVOICE_NO
    assert inv["invoice_date"] and inv["currency"]
    assert inv["source_document_id"] and inv["source_file"] == "sample_invoice.pdf"
    assert rec["source_line"] == f"manual:{rec['item_id']}"
    assert rec["line_type"] == "goods" and rec["manual_review_addition"] is True
    # bound row counts toward its authoritative invoice in the roster
    roster = r["critical_review"]["invoice_roster"]
    assert roster[0]["item_count"] == len(_rows(review)) + 1


def test_add_manual_confirmed_without_invoice(client):
    job_id, _ = _reviewed_job(client)
    r = _add(client, job_id, 1, manual=True)          # empty template item
    assert r.status_code == 200 and r.json()["status"] == "ok"
    # incomplete manual row is flagged at review time
    codes = {w["code"] for w in r.json()["critical_review"]["warnings"]}
    assert "MANUAL_ITEM_INCOMPLETE" in codes


def test_add_rejected_without_invoice_or_confirmation(client):
    job_id, _ = _reviewed_job(client)
    r = _add(client, job_id, 1, manual=False)
    assert r.status_code == 422
    assert r.json()["code"] == "MANUAL_CONFIRMATION_REQUIRED"
    r2 = _add(client, job_id, 1, invoice_id="NOT-A-REAL-INVOICE", manual=False)
    assert r2.status_code == 422 and r2.json()["code"] == "INVOICE_UNKNOWN"


def test_add_rejects_client_supplied_item_id(client):
    job_id, _ = _reviewed_job(client)
    r = client.post(f"/api/jobs/{job_id}/items", json={
        "insertion_sn": 1, "manual_review_addition": True, "item_id": "man:evil"})
    assert r.status_code == 422                       # extra field forbidden
    r2 = client.post(f"/api/jobs/{job_id}/items", json={
        "insertion_sn": 1, "manual_review_addition": True,
        "item": {"item_id": "man:evil"}})
    assert r2.status_code == 422


def test_add_generates_unique_server_ids(client):
    job_id, _ = _reviewed_job(client)
    a = _add(client, job_id, 1).json()["added_item_id"]
    b = _add(client, job_id, 1).json()["added_item_id"]
    assert a != b and a.startswith("man:") and b.startswith("man:")


def test_add_audit_event_and_revision(client):
    job_id, review = _reviewed_job(client)
    assert review["item_mutation_revision"] == 0
    r = _add(client, job_id, 2, item=GOOD_SEED).json()
    assert r["revision"] == 1
    assert r["critical_review"]["item_mutation_revision"] == 1

    db = SessionLocal()
    try:
        events = [e for e in db.query(AuditEvent).filter_by(
            job_id=job_id, event_code="ITEM_ADDED")]
    finally:
        db.close()
    assert len(events) == 1
    p = events[0].payload
    assert p["event_id"] and p["item_id"] == r["added_item_id"]
    assert p["old_sn"] is None and p["new_sn"] == 2 and p["at"]
    # The SIGNED-IN operator, not the placeholder "reviewer" this used to
    # assert: the actor column exists to answer "who changed this declaration?"
    # and a constant cannot. See tests/test_audit_actor.py.
    assert events[0].actor == os.environ["EASYCUSTOMS_AUTH_USERNAME"]
    n = len(_rows(review))
    assert len(p["before_ordering"]) == n and len(p["after_ordering"]) == n + 1
    assert p["after_ordering"][1] == r["added_item_id"]
    assert _add(client, job_id, 1).json()["revision"] == 2


def test_add_rejected_before_review_state(client):
    job_id = client.post("/api/jobs/demo").json()["job_id"]   # EXTRACTION_COMPLETE
    r = _add(client, job_id, 1)
    assert r.status_code == 409
    assert r.json()["code"] == "JOB_STATE_NOT_REVIEWABLE"


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #
def test_delete_source_item_writes_tombstone(client):
    job_id, review = _reviewed_job(client)
    rows = _rows(review)
    target = rows[1]                                   # SN 2, a source row
    r = _delete(client, job_id, target["item_id"], 2).json()
    assert r["status"] == "ok" and r["deleted_sn"] == 2
    assert r["deleted_item_id"] == target["item_id"]
    new_rows = _rows(r["critical_review"])
    assert len(new_rows) == len(rows) - 1
    assert target["item_id"] not in {x["item_id"] for x in new_rows}
    assert [x["sn"] for x in new_rows] == list(range(1, len(rows)))

    ts = _overlay(job_id)["tombstones"]
    assert len(ts) == 1
    t = ts[0]
    assert t["item_id"] == target["item_id"] and t["origin"] == "source"
    assert t["invoice_no"] == DEMO_INVOICE_NO and t["invoice_date"]
    assert t["description"] == target["description"]
    assert t["previous_sn"] == 2 and t["deleted_at"]
    assert t["invoice_line_index"] >= 0
    # evidence untouched: the extracted invoice still holds every source row
    db = SessionLocal()
    try:
        docs = db.get(Job, job_id).documents
        inv_doc = next(d for d in docs if d.declared_role == "INVOICE")
        assert len(inv_doc.raw_extraction["rows"]) == 119
    finally:
        db.close()


def test_delete_manual_item_marked_inactive(client):
    job_id, _ = _reviewed_job(client)
    added = _add(client, job_id, 1, item=GOOD_SEED).json()
    item_id = added["added_item_id"]
    r = _delete(client, job_id, item_id, 1).json()
    assert r["status"] == "ok"
    rec = next(m for m in _overlay(job_id)["manual_items"] if m["item_id"] == item_id)
    assert rec["active"] is False and rec["deleted_at"]
    assert not _overlay(job_id)["tombstones"]          # manual rows never tombstone
    assert item_id not in {x["item_id"] for x in _rows(r["critical_review"])}


def test_delete_requires_exact_sn(client):
    job_id, review = _reviewed_job(client)
    target = _rows(review)[4]                          # SN 5
    r = _delete(client, job_id, target["item_id"], 6)
    assert r.status_code == 409
    assert r.json()["code"] == "CONFIRMATION_SN_MISMATCH"
    # nothing changed
    fresh = client.get(f"/api/jobs/{job_id}/critical-review").json()
    assert len(_rows(fresh)) == len(_rows(review))
    assert fresh["item_mutation_revision"] == 0


def test_delete_rejects_stale_sn_after_reorder(client):
    job_id, review = _reviewed_job(client)
    rows = _rows(review)
    a, b = rows[1], rows[2]                            # SN 2 and SN 3
    assert _delete(client, job_id, a["item_id"], 2).json()["status"] == "ok"
    # b is now SN 2 — its old SN must be refused
    r = _delete(client, job_id, b["item_id"], 3)
    assert r.status_code == 409 and r.json()["code"] == "CONFIRMATION_SN_MISMATCH"
    assert "SN 2" in r.json()["message"]
    assert _delete(client, job_id, b["item_id"], 2).json()["status"] == "ok"


def test_delete_unknown_item_404(client):
    job_id, _ = _reviewed_job(client)
    r = _delete(client, job_id, "src:doesnotexist00", 1)
    assert r.status_code == 404 and r.json()["code"] == "ITEM_NOT_FOUND"


def test_delete_last_goods_item_refused(client):
    # single-item job: demo invoice fixture trimmed to one goods row
    job_id = client.post("/api/jobs").json()["job_id"]
    fx = json.loads((SAMPLE_DIR / "fixtures" / "invoice.json").read_text())
    fx["rows"] = fx["rows"][:1]
    fx["totals"] = None
    pdf = (SAMPLE_DIR / "sample_invoice.pdf").read_bytes()
    up = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                     files={"file": ("inv.pdf", pdf, "application/pdf")},
                     data={"fixture": json.dumps(fx)})
    assert up.status_code == 200
    review = client.get(f"/api/jobs/{job_id}/critical-review").json()
    rows = _rows(review)
    assert len(rows) == 1
    r = _delete(client, job_id, rows[0]["item_id"], 1)
    assert r.status_code == 409 and r.json()["code"] == "LAST_ITEM_UNDELETABLE"


def test_deleted_identity_never_reactivated(client):
    job_id, _ = _reviewed_job(client)
    first = _add(client, job_id, 1, item=GOOD_SEED).json()["added_item_id"]
    assert _delete(client, job_id, first, 1).json()["status"] == "ok"
    # identical seed again -> a brand-new identity, old one stays inactive
    second = _add(client, job_id, 1, item=GOOD_SEED).json()["added_item_id"]
    assert second != first
    recs = {m["item_id"]: m for m in _overlay(job_id)["manual_items"]}
    assert recs[first]["active"] is False and recs[second]["active"] is True
    # deleting the dead identity again is a 404, not a reactivation
    r = _delete(client, job_id, first, 1)
    assert r.status_code == 404
    assert _overlay(job_id)["revision"] == 3           # add, delete, add — no 4th


def test_delete_audit_event(client):
    job_id, review = _reviewed_job(client)
    target = _rows(review)[0]
    _delete(client, job_id, target["item_id"], 1)
    db = SessionLocal()
    try:
        ev = db.query(AuditEvent).filter_by(job_id=job_id, event_code="ITEM_DELETED").one()
    finally:
        db.close()
    p = ev.payload
    assert p["item_id"] == target["item_id"]
    assert p["old_sn"] == 1 and p["new_sn"] is None and p["event_id"]
    assert len(p["before_ordering"]) == len(p["after_ordering"]) + 1


# --------------------------------------------------------------------------- #
# Staleness, invariants, recalculation
# --------------------------------------------------------------------------- #
def test_mutation_invalidates_xml_and_declaration(client):
    job_id, review = _reviewed_job(client)
    body = dict(FINALIZE_BODY, review_fingerprint=review["review_fingerprint"])
    fin = client.post(f"/api/jobs/{job_id}/finalize", json=body)
    assert fin.status_code == 200 and fin.json()["ready_for_xml"] is True
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 200

    r = _add(client, job_id, 1, item=GOOD_SEED)        # mutate from XML_READY
    assert r.status_code == 200
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 404
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "CRITICAL_REVIEW_REQUIRED"
    assert job["has_declaration"] is False


def test_mutation_stales_review_fingerprint(client):
    job_id, review = _reviewed_job(client)
    old_fp = review["review_fingerprint"]
    r = _add(client, job_id, 1, item=GOOD_SEED).json()
    assert r["critical_review"]["review_fingerprint"] != old_fp
    # finalizing against the pre-mutation review is refused as stale
    fin = client.post(f"/api/jobs/{job_id}/finalize",
                      json=dict(FINALIZE_BODY, review_fingerprint=old_fp)).json()
    assert fin["status"] == "REVIEW_STALE"


def test_derived_fields_recalculated(client):
    job_id, review = _reviewed_job(client)
    old_total = Decimal(review["calculated_goods_total"])
    r = _add(client, job_id, 3, invoice_id=DEMO_INVOICE_NO, manual=False,
             item=GOOD_SEED).json()
    cr = r["critical_review"]
    assert Decimal(cr["calculated_goods_total"]) == old_total + Decimal("100.00")
    assert cr["invoice_item_count"] == len(_rows(review)) + 1
    row = next(x for x in _rows(cr) if x["item_id"] == r["added_item_id"])
    assert row["sn"] == 3 and row["origin"] == "manual"
    assert row["final_hs"] == "85044090900"            # DB-gated resolution ran
    assert row["coo"] == "CN"
    # allocation ran: the unmatched-by-packing manual row gets the minimum
    # floor (net + 0.0001) under packing-gross Condition 1 — small but real
    assert Decimal(row["gross"]) > 0 and Decimal(row["net"]) >= 0
    assert row["sup_unit"] and Decimal(row["sup_qty"]) > 0         # supplementary ran
    # allocation preview still reconciles exactly to the shipment authority
    assert sum(Decimal(x["gross"]) for x in _rows(cr)) == Decimal("199")


def test_corrupt_overlay_duplicate_id_rejected(client):
    job_id, review = _reviewed_job(client)
    _add(client, job_id, 1, item=GOOD_SEED)
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        bad = dict(job.item_mutations)
        bad["ordered_item_ids"] = bad["ordered_item_ids"] + [bad["ordered_item_ids"][0]]
        job.item_mutations = bad
        db.commit()
    finally:
        db.close()
    r = _add(client, job_id, 1)
    assert r.status_code == 409
    assert r.json()["code"] in ("ITEM_ORDER_INVALID", "ITEM_ID_DUPLICATE")


def test_incomplete_manual_item_warns_but_xml_still_generated(client):
    """Warn-mode (user rule 2026-07-18): blocking cases never stop the XML —
    the validation verdict stays honest, the cases are returned for the
    pop-up, and the XML is downloadable for real-ASYCUDA testing."""
    from lxml import etree

    job_id, _ = _reviewed_job(client)
    r = _add(client, job_id, 1).json()                 # empty template row
    fp = r["critical_review"]["review_fingerprint"]
    fin = client.post(f"/api/jobs/{job_id}/finalize",
                      json=dict(FINALIZE_BODY, review_fingerprint=fp))
    assert fin.status_code == 200                      # warn, not block
    decl = fin.json()
    codes = {m["code"] for m in decl["blocking_errors"]}
    assert "MANUAL_ITEM_INCOMPLETE" in codes
    assert decl["ready_for_xml"] is False              # verdict unchanged
    assert decl["xml_built_with_blockers"] is True
    xml = client.get(f"/api/jobs/{job_id}/xml")
    assert xml.status_code == 200
    root = etree.fromstring(xml.content)
    assert len(root.findall("Item")) == 120            # incomplete row included
    # unresolved HS serialized as EMPTY, never invented
    assert (root.findall("Item")[0].findtext("Tarification/HScode/Commodity_code") or "") == ""
    db = SessionLocal()
    try:
        assert db.query(AuditEvent).filter_by(
            job_id=job_id, event_code="XML_BUILT_WITH_BLOCKERS").count() == 1
    finally:
        db.close()


def test_strict_mode_still_blocks_xml(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "xml_strict_blocking", True)
    job_id, _ = _reviewed_job(client)
    r = _add(client, job_id, 1).json()                 # empty template row
    fin = client.post(f"/api/jobs/{job_id}/finalize", json=dict(
        FINALIZE_BODY, review_fingerprint=r["critical_review"]["review_fingerprint"]))
    assert fin.status_code == 409
    assert fin.json()["ready_for_xml"] is False
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 404
