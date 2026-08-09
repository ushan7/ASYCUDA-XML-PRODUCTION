"""Reviewer edits of item invoice fields in the Detailed Review
(description / COO / quantity / UOM / total price — keyed by immutable
item_id; evidence immutable; every derived value recomputed)."""
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


def _edit(client, job_id, item_id, **fields):
    return client.patch(f"/api/jobs/{job_id}/items/{item_id}",
                        json={"fields": fields})


def test_edit_fields_recompute_everything(client):
    job_id, review = _reviewed_job(client)
    row = review["item_details"][0]
    old_total = Decimal(review["calculated_goods_total"])
    old_price = Decimal(row["total_price"])
    r = _edit(client, job_id, row["item_id"], description="Edited widget",
              quantity=40, uom="SET", total_price=99.5, country_of_origin="Japan")
    assert r.status_code == 200
    cr = r.json()["critical_review"]
    new_row = next(x for x in cr["item_details"] if x["item_id"] == row["item_id"])
    assert new_row["description"] == "Edited widget" and new_row["edited"] is True
    assert new_row["quantity"] == "40" and new_row["uom"] == "SET"
    assert new_row["total_price"] == "99.50"
    assert new_row["coo"] == "JP"                    # COO re-resolved from "Japan"
    assert Decimal(new_row["sup_qty"]) > 0           # supplementary recomputed
    # goods total follows the edited price
    assert Decimal(cr["calculated_goods_total"]) == old_total - old_price + Decimal("99.50")
    # allocation still reconciles exactly to the shipment authority
    assert sum(Decimal(x["gross"]) for x in cr["item_details"]) == Decimal("199")
    assert cr["item_mutation_revision"] == 1
    db = SessionLocal()
    try:
        ev = db.query(AuditEvent).filter_by(job_id=job_id,
                                            event_code="ITEM_FIELDS_EDITED").one()
        assert ev.payload["before"]["description"] and ev.payload["changed"]["uom"] == "SET"
        # evidence untouched
        inv_doc = next(d for d in db.get(Job, job_id).documents
                       if d.declared_role == "INVOICE")
        assert inv_doc.raw_extraction["rows"][0]["description_raw"] != "Edited widget"
    finally:
        db.close()


def test_partial_edits_merge(client):
    job_id, review = _reviewed_job(client)
    item_id = review["item_details"][2]["item_id"]
    assert _edit(client, job_id, item_id, description="First pass").status_code == 200
    r = _edit(client, job_id, item_id, quantity=7)
    row = next(x for x in r.json()["critical_review"]["item_details"]
               if x["item_id"] == item_id)
    assert row["description"] == "First pass" and row["quantity"] == "7"


def test_edit_validation_rejections(client):
    job_id, review = _reviewed_job(client)
    item_id = review["item_details"][0]["item_id"]
    assert _edit(client, job_id, "src:unknown0000", description="x").status_code == 404
    r = _edit(client, job_id, item_id, description="   ")
    assert r.status_code == 422 and r.json()["code"] == "DESCRIPTION_REQUIRED"
    for bad in (0, -2, "nan", "abc"):
        assert _edit(client, job_id, item_id, quantity=bad).status_code == 422
    assert _edit(client, job_id, item_id, total_price=-1).status_code == 422
    assert _edit(client, job_id, item_id).status_code == 422       # no fields
    # supplementary quantity: a positive number only (0 / negative / unreadable)
    for bad in (0, -3, "nan", "abc"):
        r = _edit(client, job_id, item_id, supplementary_quantity=bad)
        assert r.status_code == 422 and r.json()["code"] == "SUPPLEMENTARY_QUANTITY_INVALID"
    # non-editable / unknown fields are rejected by the request model
    # (gross/net weight and cartons became reviewer-editable pins, and the
    # supplementary QUANTITY an override 2026-08-04 — the supplementary unit
    # CODE stays derived from the tariff unit of the final HS)
    r = client.patch(f"/api/jobs/{job_id}/items/{item_id}",
                     json={"fields": {"final_hs_code": "85044090900"}})
    assert r.status_code == 422
    r = client.patch(f"/api/jobs/{job_id}/items/{item_id}",
                     json={"fields": {"package_count": "5"}})
    assert r.status_code == 422
    # review untouched by all the rejections
    fresh = client.get(f"/api/jobs/{job_id}/critical-review").json()
    assert fresh["item_mutation_revision"] == 0


def test_edits_survive_recompute_and_reorder_and_deletion_cleanup(client):
    job_id, review = _reviewed_job(client)
    rows = review["item_details"]
    target, first = rows[3], rows[0]
    assert _edit(client, job_id, target["item_id"], description="Sticky edit").status_code == 200
    # reorder via delete — the edit follows the item_id, not the SN
    assert client.request("DELETE", f"/api/jobs/{job_id}/items/{first['item_id']}",
                          json={"confirmation_sn": "1"}).status_code == 200
    fresh = client.get(f"/api/jobs/{job_id}/critical-review").json()
    moved = next(x for x in fresh["item_details"] if x["item_id"] == target["item_id"])
    assert moved["sn"] == 3 and moved["description"] == "Sticky edit" and moved["edited"]
    # deleting the edited row drops its stored edits
    assert client.request("DELETE", f"/api/jobs/{job_id}/items/{target['item_id']}",
                          json={"confirmation_sn": "3"}).status_code == 200
    db = SessionLocal()
    try:
        overlay = db.get(Job, job_id).item_mutations
        assert target["item_id"] not in (overlay.get("field_edits") or {})
    finally:
        db.close()


def test_edit_manual_item_and_fingerprint_staleness(client):
    job_id, review = _reviewed_job(client)
    old_fp = review["review_fingerprint"]
    added = client.post(f"/api/jobs/{job_id}/items", json={
        "insertion_sn": 1, "manual_review_addition": True,
        "item": {"description": "Manual row", "quantity": 2, "uom": "PCS",
                 "total_price": 10, "country_of_origin": "CN",
                 "final_hs_code": "85044090900"}}).json()
    r = _edit(client, job_id, added["added_item_id"], quantity=6, total_price=30)
    assert r.status_code == 200
    row = next(x for x in r.json()["critical_review"]["item_details"]
               if x["item_id"] == added["added_item_id"])
    assert row["quantity"] == "6" and row["total_price"] == "30.00"
    assert row["origin"] == "manual" and row["edited"] is True
    # any pre-edit review is stale at finalize
    fin = client.post(f"/api/jobs/{job_id}/finalize",
                      json=dict(FINALIZE_BODY, review_fingerprint=old_fp)).json()
    assert fin["status"] == "REVIEW_STALE"


def test_edited_content_flows_into_final_xml(client):
    """User rule 2026-07-18: everything the reviewer edited is FINAL for XML —
    the reviewed HS code with ITS official unit (UNT->KGM flips supplementary
    to net weight instead of count), the edited description/qty/UOM composed
    into the commercial description, and the edited COO."""
    from decimal import Decimal as D

    from lxml import etree

    from app.reference.store import get_reference

    job_id, review = _reviewed_job(client)
    row = review["item_details"][0]
    assert row["sup_unit"] == "UNT" and row["sup_qty"] == row["quantity"]   # before

    # reviewer edits the invoice fields...
    assert _edit(client, job_id, row["item_id"], description="Copper Winding Wire",
                 quantity=5, uom="ROLL", total_price=100,
                 country_of_origin="Japan").status_code == 200
    # ...and selects a KGM-unit HS through the DB-gated review channel
    ref = get_reference()
    kgm = next(c for c, r in sorted(ref.hs_by_11.items()) if r.unit == "KGM")
    assert client.post(f"/api/jobs/{job_id}/items/hs-review", json={
        "item_id": row["item_id"], "final_hs_code": kgm,
        "hs_review_source": "detailed_review_hs_search"}).status_code == 200

    fresh = client.get(f"/api/jobs/{job_id}/critical-review").json()
    fin = client.post(f"/api/jobs/{job_id}/finalize",
                      json=dict(FINALIZE_BODY,
                                review_fingerprint=fresh["review_fingerprint"]))
    assert fin.status_code == 200
    decl = fin.json()
    assert decl["ready_for_xml"] is True
    it = decl["items"][0]

    # reviewed HS is final, split 8+3
    assert it["hs_code_11"] == kgm
    assert it["commodity_code"] == kgm[:8] and it["precision_1"] == kgm[8:]
    assert it["hs_source"] == "DETAILED_REVIEW_HS_SEARCH"
    # supplementary follows the NEW code's official unit: KGM -> net weight,
    # NOT the piece count
    assert it["supplementary_unit_code"] == "KGM"
    assert D(it["supplementary_quantity"]) == D(it["net_weight_kg"])
    assert D(it["supplementary_quantity"]) != D("5")
    # commercial description = edited desc + edited qty + edited UOM
    assert it["commercial_description"] == "Copper Winding Wire 5 ROLL"
    # official goods description comes from the reviewed code's DB record
    assert it["goods_description"] == ref.hs_by_11[kgm].description
    assert it["coo_alpha2"] == "JP"

    # and the SAME values appear in the generated XML
    xml = client.get(f"/api/jobs/{job_id}/xml")
    assert xml.status_code == 200
    root = etree.fromstring(xml.content)
    x = root.findall("Item")[0]
    assert x.findtext("Tarification/HScode/Commodity_code") == kgm[:8]
    assert x.findtext("Tarification/HScode/Precision_1") == kgm[8:]
    sup = x.findall("Tarification/Supplementary_unit")[0]
    assert sup.findtext("Suppplementary_unit_code") == "KGM"
    assert D(sup.findtext("Suppplementary_unit_quantity")) == D(it["net_weight_kg"])
    assert x.findtext("Goods_description/Commercial_Description") == "Copper Winding Wire 5 ROLL"
    assert x.findtext("Goods_description/Description_of_goods") == ref.hs_by_11[kgm].description
    assert x.findtext("Goods_description/Country_of_origin_code") == "JP"


def test_supplementary_quantity_override_flows_into_xml_and_clears(client):
    """User rule 2026-08-04: the reviewer can override one row's supplementary
    QUANTITY outright, and that number is what the XML carries.  It is an
    override and nothing more — the derivation rules are untouched, the unit
    code still comes from the tariff unit of the final HS, the values it was
    derived from do not move, and no other row is rebalanced (unlike the
    weight/carton pins, there is no shipment authority above this field).
    Clearing the box brings the derived quantity back."""
    from decimal import Decimal as D

    from lxml import etree

    job_id, review = _reviewed_job(client)
    rows = review["item_details"]
    row, sibling = rows[0], rows[1]
    derived = D(row["sup_qty"])
    assert derived > 0 and row["sup_qty_pinned"] is False

    override = derived + D("7.5")
    assert _edit(client, job_id, row["item_id"],
                 supplementary_quantity=str(override)).status_code == 200
    fresh = client.get(f"/api/jobs/{job_id}/critical-review").json()
    new_row = next(x for x in fresh["item_details"] if x["item_id"] == row["item_id"])
    assert D(new_row["sup_qty"]) == override and new_row["sup_qty_pinned"] is True
    # unit code, and the qty/net the derivation reads, are all untouched
    assert new_row["sup_unit"] == row["sup_unit"] and new_row["sup_name"] == row["sup_name"]
    assert new_row["quantity"] == row["quantity"] and new_row["net"] == row["net"]
    # no sibling row absorbed anything
    new_sib = next(x for x in fresh["item_details"] if x["item_id"] == sibling["item_id"])
    assert new_sib["sup_qty"] == sibling["sup_qty"] and new_sib["sup_qty_pinned"] is False

    fin = client.post(f"/api/jobs/{job_id}/finalize",
                      json=dict(FINALIZE_BODY,
                                review_fingerprint=fresh["review_fingerprint"]))
    assert fin.status_code == 200
    decl = fin.json()
    assert decl["ready_for_xml"] is True
    it = decl["items"][0]
    assert D(it["supplementary_quantity"]) == override
    assert it["supplementary_unit_code"] == row["sup_unit"]

    xml = client.get(f"/api/jobs/{job_id}/xml")
    assert xml.status_code == 200
    sup = etree.fromstring(xml.content).findall("Item")[0].findall(
        "Tarification/Supplementary_unit")[0]
    assert D(sup.findtext("Suppplementary_unit_quantity")) == override
    assert sup.findtext("Suppplementary_unit_code") == row["sup_unit"]

    # clearing the box restores the derived quantity
    assert _edit(client, job_id, row["item_id"],
                 supplementary_quantity="").status_code == 200
    back = client.get(f"/api/jobs/{job_id}/critical-review").json()
    cleared = next(x for x in back["item_details"] if x["item_id"] == row["item_id"])
    assert D(cleared["sup_qty"]) == derived and cleared["sup_qty_pinned"] is False


def test_field40_candidates_and_mandatory_confirmation(client):
    """Rule 2: the system suggests a Field 40 value but ALWAYS asks — an
    unconfirmed choice is a blocking case (warn-mode: pop-up + test XML);
    the reviewer's confirmed choice is final and stamped on every item."""
    from lxml import etree

    job_id, review = _reviewed_job(client)
    cands = review["field_40_candidates"]
    assert {c["source"] for c in cands} == {"HAWB", "MAWB"}
    hawb = next(c for c in cands if c["source"] == "HAWB")
    assert hawb["value"] == "DEMOHAWB0057"
    assert hawb["suggested"] is True                  # 199 kg < 330 kg -> HAWB
    assert review["field_40_previous_document"] == "DEMOHAWB0057"

    # finalize WITHOUT confirming -> blocking case, XML still built (warn-mode)
    body = {k: v for k, v in FINALIZE_BODY.items() if k != "field_40_confirmed"}
    fin = client.post(f"/api/jobs/{job_id}/finalize",
                      json=dict(body, review_fingerprint=review["review_fingerprint"]))
    assert fin.status_code == 200
    decl = fin.json()
    assert decl["ready_for_xml"] is False
    assert any(m["code"] == "FIELD_40_UNCONFIRMED" for m in decl["blocking_errors"])

    # the reviewer picks the MAWB number and confirms -> their choice is FINAL
    fin = client.post(f"/api/jobs/{job_id}/finalize", json=dict(
        FINALIZE_BODY, review_fingerprint=review["review_fingerprint"],
        field_40_previous_document="555-12345678", field_40_confirmed=True))
    assert fin.status_code == 200 and fin.json()["ready_for_xml"] is True
    root = etree.fromstring(client.get(f"/api/jobs/{job_id}/xml").content)
    stamped = {i.findtext("Previous_doc/Summary_declaration").strip()
               for i in root.findall("Item")}
    assert stamped == {"555-12345678"}                # user choice on every item


def test_edit_invalidates_xml(client):
    job_id, review = _reviewed_job(client)
    fin = client.post(f"/api/jobs/{job_id}/finalize",
                      json=dict(FINALIZE_BODY,
                                review_fingerprint=review["review_fingerprint"]))
    assert fin.status_code == 200
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 200
    item_id = review["item_details"][0]["item_id"]
    assert _edit(client, job_id, item_id, quantity=99).status_code == 200
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 404
