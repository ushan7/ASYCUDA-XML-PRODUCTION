"""Reviewer-edited per-item gross/net weights in the Detailed Review
(user rule 2026-07-19): entered values PIN the item — allocation keeps them
exact and redistributes the remaining authorised gross across unpinned items,
still exact-sum reconciled; impossible pins block instead of silently
rescaling."""
from decimal import Decimal

import pytest

from fastapi.testclient import TestClient

from app.database import init_db
from app.main import app
from app.rules.models import WorkItem
from app.rules.weight_carton import (
    REVIEWED_GROSS_EXCEEDS_MSG,
    REVIEWED_GROSS_MISMATCH_MSG,
    allocate_weights_and_cartons,
)

D = Decimal


def _item(seq, desc="Widget", qty="1", total="100"):
    return WorkItem(
        xml_item_sequence=seq, source_invoice_number="INV-1", source_invoice_date="",
        source_invoice_item_index=seq, source_invoice_item_no=None,
        description_raw=desc, quantity=D(qty), invoice_uom_raw="PCS",
        unit_price=D("1"), line_total=D(total), currency="USD")


# --------------------------------------------------------------------------- #
# Engine: pinning semantics
# --------------------------------------------------------------------------- #
def test_pinned_gross_kept_exact_and_rest_redistributed():
    items = [_item(1, "ALPHA"), _item(2, "BETA"), _item(3, "GAMMA")]
    items[1].manual_gross_weight_kg = D("50")
    msgs = allocate_weights_and_cartons(items, {}, D("100"), D("10"), packing_present=False)
    assert items[1].gross_weight_kg == D("50.0000")                  # pinned EXACT
    assert sum(i.gross_weight_kg for i in items) == D("100.0000")    # still reconciled
    assert items[0].gross_weight_kg == items[2].gross_weight_kg == D("25.0000")
    assert items[1].allocation_audit["gross_weight_source"] == "reviewer-entered gross weight"
    assert any(m.code == "ITEM_WEIGHTS_REVIEWED" for m in msgs)
    assert not [m for m in msgs if m.severity.value == "BLOCKING"]


def test_pinned_net_used_as_top_priority():
    items = [_item(1, "ALPHA"), _item(2, "BETA")]
    items[0].item_weight_kg = D("9")             # invoice weight would win normally...
    items[0].item_weight_scope = "TOTAL"
    items[0].manual_net_weight_kg = D("5")       # ...but the reviewer's net outranks it
    msgs = allocate_weights_and_cartons(items, {}, D("100"), D("10"), packing_present=False)
    assert items[0].net_weight_kg == D("5.0000")
    assert items[0].allocation_audit["net_weight_source"] == "reviewer-entered net weight"
    assert items[0].net_weight_kg < items[0].gross_weight_kg
    assert sum(i.gross_weight_kg for i in items) == D("100.0000")
    assert not [m for m in msgs if m.severity.value == "BLOCKING"]


def test_ratio_net_follows_pinned_gross():
    items = [_item(1, "ALPHA"), _item(2, "BETA")]
    items[0].manual_gross_weight_kg = D("40")
    allocate_weights_and_cartons(items, {}, D("100"), D("10"), packing_present=False)
    assert items[0].gross_weight_kg == D("40.0000")
    assert items[0].net_weight_kg == D("28.000")         # 0.7 x the PINNED gross
    assert items[1].gross_weight_kg == D("60.0000")      # remainder, exact
    assert sum(i.gross_weight_kg for i in items) == D("100.0000")


def test_pins_exceeding_authority_block():
    items = [_item(1, "ALPHA"), _item(2, "BETA"), _item(3, "GAMMA")]
    items[0].manual_gross_weight_kg = D("80")
    items[1].manual_gross_weight_kg = D("70")            # 150 > 100 authorised
    msgs = allocate_weights_and_cartons(items, {}, D("100"), D("10"), packing_present=False)
    blocking = [m for m in msgs if m.severity.value == "BLOCKING"]
    assert any(m.code == "REVIEWED_GROSS_EXCEEDS_AUTHORITY" for m in blocking)
    assert any(REVIEWED_GROSS_EXCEEDS_MSG in str(m) for m in blocking)
    assert items[0].gross_weight_kg == D("80.0000")      # pins kept for display
    assert items[1].gross_weight_kg == D("70.0000")


def test_all_pinned_must_sum_to_authority():
    items = [_item(1, "ALPHA"), _item(2, "BETA")]
    items[0].manual_gross_weight_kg = D("30")
    items[1].manual_gross_weight_kg = D("50")            # 80 != 100
    msgs = allocate_weights_and_cartons(items, {}, D("100"), D("10"), packing_present=False)
    assert any(m.code == "REVIEWED_GROSS_TOTAL_MISMATCH" and REVIEWED_GROSS_MISMATCH_MSG in str(m)
               for m in msgs if m.severity.value == "BLOCKING")

    items2 = [_item(1, "ALPHA"), _item(2, "BETA")]
    items2[0].manual_gross_weight_kg = D("30")
    items2[1].manual_gross_weight_kg = D("70")           # exact -> fine
    msgs2 = allocate_weights_and_cartons(items2, {}, D("100"), D("10"), packing_present=False)
    assert not [m for m in msgs2 if m.severity.value == "BLOCKING"]
    assert [i.gross_weight_kg for i in items2] == [D("30.0000"), D("70.0000")]
    assert [i.package_count for i in items2] == [D("3.00"), D("7.00")]   # cartons follow gross


def test_no_pins_path_unchanged():
    items = [_item(1, "ALPHA", total="30"), _item(2, "BETA", total="70")]
    msgs = allocate_weights_and_cartons(items, {}, D("100"), D("10"))
    assert [i.gross_weight_kg for i in items] == [D("30.0000"), D("70.0000")]
    assert not any(m.code == "ITEM_WEIGHTS_REVIEWED" for m in msgs)


# --------------------------------------------------------------------------- #
# API: edit channel + Detailed Review preview
# --------------------------------------------------------------------------- #
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
    return client.patch(f"/api/jobs/{job_id}/items/{item_id}", json={"fields": fields})


def test_weight_edits_flow_into_review_preview(client):
    job_id, review = _reviewed_job(client)
    row1, row2 = review["item_details"][0], review["item_details"][1]

    r = _edit(client, job_id, row1["item_id"], gross_weight=25)
    assert r.status_code == 200
    cr = r.json()["critical_review"]
    new1 = next(x for x in cr["item_details"] if x["item_id"] == row1["item_id"])
    assert new1["gross"] == "25" and new1["gross_pinned"] is True
    assert new1["edited"] is True
    # authority total still reconciles exactly (demo: 199 kg)
    assert sum(D(x["gross"]) for x in cr["item_details"]) == D("199")
    assert any(w["code"] == "ITEM_WEIGHTS_REVIEWED" for w in cr["warnings"])

    r2 = _edit(client, job_id, row2["item_id"], net_weight="0.9")
    assert r2.status_code == 200
    cr2 = r2.json()["critical_review"]
    new2 = next(x for x in cr2["item_details"] if x["item_id"] == row2["item_id"])
    assert new2["net"] == "0.9" and new2["net_pinned"] is True
    assert D(new2["net"]) < D(new2["gross"])
    assert sum(D(x["gross"]) for x in cr2["item_details"]) == D("199")


def test_weight_edit_validation(client):
    job_id, review = _reviewed_job(client)
    item_id = review["item_details"][0]["item_id"]

    r = _edit(client, job_id, item_id, gross_weight=0)
    assert r.status_code == 422 and r.json()["code"] == "GROSS_WEIGHT_INVALID"
    r = _edit(client, job_id, item_id, net_weight="-3")
    assert r.status_code == 422 and r.json()["code"] == "NET_WEIGHT_INVALID"
    r = _edit(client, job_id, item_id, gross_weight=5, net_weight=6)
    assert r.status_code == 422 and r.json()["code"] == "NET_NOT_BELOW_GROSS"

    # merged-pair check: pin gross first, then a conflicting net alone
    assert _edit(client, job_id, item_id, gross_weight=5).status_code == 200
    r = _edit(client, job_id, item_id, net_weight=6)
    assert r.status_code == 422 and r.json()["code"] == "NET_NOT_BELOW_GROSS"
    # a compatible net is accepted and both pins show in the preview
    r = _edit(client, job_id, item_id, net_weight="4.5")
    assert r.status_code == 200
    row = next(x for x in r.json()["critical_review"]["item_details"]
               if x["item_id"] == item_id)
    assert row["gross"] == "5" and row["net"] == "4.5"
    assert row["gross_pinned"] is True and row["net_pinned"] is True
