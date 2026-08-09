"""Reviewer-edited per-item CARTON counts in the Detailed Review (user rule
2026-08-03).

The contract, and why it is not simply the weight contract with a different
column name:

* every declared CTN is >= 0.01 AND a whole multiple of 0.01 (the MUST rule) —
  so a pin's effect is a whole number of 0.01 units and the redistribution is
  exact integer arithmetic;
* a human-entered value is ALWAYS accepted; the system adjusts the other items
  to hold the authority, and where that is impossible the pins still stand and
  the message says what the shipment total would have to become;
* ESTIMATED rows absorb the difference first, concentrated into at most
  CTN_DONOR_MAX of them, so one edit does not churn the whole table;
* PACKING-STATED rows are rescaled proportionally as a set — preserving every
  ratio between them — and only when the estimates cannot cover it.
"""
import time
from decimal import Decimal

import pytest

from fastapi.testclient import TestClient

from app.database import init_db
from app.main import app
from app.rules.models import WorkItem
from app.rules.packing_match import PackingEvidence
from app.rules.weight_carton import (
    CTN_DONOR_MAX,
    REVIEWED_CTN_EXCEEDS_MSG,
    REVIEWED_CTN_MISMATCH_MSG,
    allocate_weights_and_cartons,
)

D = Decimal
_MIN = D("0.01")


def _item(seq, desc="Widget", qty="1", total="100"):
    return WorkItem(
        xml_item_sequence=seq, source_invoice_number="INV-1", source_invoice_date="",
        source_invoice_item_index=seq, source_invoice_item_no=None,
        description_raw=desc, quantity=D(qty), invoice_uom_raw="PCS",
        unit_price=D("1"), line_total=D(total), currency="USD")


def _items(n):
    return [_item(i, f"IT{i}") for i in range(1, n + 1)]


def _ctns(items):
    return [it.package_count for it in items]


def _codes(msgs):
    return [m.code for m in msgs]


def _blocking(msgs):
    return [m for m in msgs if m.severity.value == "BLOCKING"]


def _assert_lattice(items, auth=None):
    """The MUST rule, asserted the way the engine asserts it."""
    for it in items:
        assert it.package_count is not None
        assert it.package_count >= _MIN, f"SN {it.xml_item_sequence} below the 0.01 minimum"
        assert it.package_count % _MIN == 0, f"SN {it.xml_item_sequence} off the 0.01 lattice"
    if auth is not None:
        assert sum(_ctns(items)) == auth


# --------------------------------------------------------------------------- #
# Engine: the pin is exact, the authority holds, the table barely moves
# --------------------------------------------------------------------------- #
def test_pin_kept_exact_and_absorbed_by_top_estimates():
    items = _items(20)                                   # baseline 5.00 each of 100
    items[2].manual_package_count = D("8")
    msgs = allocate_weights_and_cartons(items, {}, D("1000"), D("100"), packing_present=False)

    assert items[2].package_count == D("8.00")           # pinned EXACT
    _assert_lattice(items, D("100"))
    moved = [i for i, c in enumerate(_ctns(items)) if i != 2 and c != D("5.00")]
    assert len(moved) == CTN_DONOR_MAX                   # only the donors moved
    assert all(items[i].package_count == D("4.70") for i in moved)
    assert items[2].allocation_audit["carton_source"] == "reviewer-entered carton count"
    assert "absorbed for reviewer carton pin" in items[moved[0]].allocation_audit["carton_source"]
    assert "ITEM_CARTONS_REVIEWED" in _codes(msgs)
    assert not _blocking(msgs)


def test_donor_set_shrinks_to_the_delta_when_it_is_smaller_than_ten():
    """Granularity: 3 units cannot be shared by 10 rows — a third of a unit is
    not on the lattice — so exactly 3 rows move (the 10 -> ... -> 1 fallback)."""
    items = _items(20)
    items[2].manual_package_count = D("5.03")            # +3 units
    allocate_weights_and_cartons(items, {}, D("1000"), D("100"), packing_present=False)

    _assert_lattice(items, D("100"))
    moved = [c for i, c in enumerate(_ctns(items)) if i != 2 and c != D("5.00")]
    assert len(moved) == 3 and all(c == D("4.99") for c in moved)


def test_donor_set_grows_past_ten_when_headroom_binds():
    """Capacity pulls the other way: when the top rows cannot give enough
    without breaching 0.01, the set extends DOWN the ordering.  Shrinking it
    (the granularity direction) would make the shortfall worse."""
    items = _items(40)                                   # baseline 1.00 each of 40
    items[0].manual_package_count = D("21")              # needs 20.00 from the rest
    msgs = allocate_weights_and_cartons(items, {}, D("400"), D("40"), packing_present=False)

    _assert_lattice(items, D("40"))
    assert items[0].package_count == D("21.00")
    donors = [c for c in _ctns(items)[1:] if c != D("1.00")]
    assert len(donors) > CTN_DONOR_MAX                   # ten rows hold only 9.90
    assert not _blocking(msgs)


def test_pin_below_baseline_adds_cartons_back():
    items = _items(20)
    items[2].manual_package_count = D("2")               # -3.00 -> others gain
    allocate_weights_and_cartons(items, {}, D("1000"), D("100"), packing_present=False)

    _assert_lattice(items, D("100"))
    assert items[2].package_count == D("2.00")
    gained = [c for i, c in enumerate(_ctns(items)) if i != 2 and c != D("5.00")]
    assert len(gained) == CTN_DONOR_MAX and all(c == D("5.30") for c in gained)


def test_two_pins_are_absorbed_as_one_delta():
    items = _items(20)
    items[2].manual_package_count = D("8")
    items[9].manual_package_count = D("2")               # net delta +0.00
    msgs = allocate_weights_and_cartons(items, {}, D("1000"), D("100"), packing_present=False)

    _assert_lattice(items, D("100"))
    assert items[2].package_count == D("8.00") and items[9].package_count == D("2.00")
    # the two pins cancel, so NOTHING else moves — pins are summed against the
    # no-pin baseline, not applied one after another
    assert all(items[i].package_count == D("5.00") for i in range(20) if i not in (2, 9))
    assert not _blocking(msgs)


# --------------------------------------------------------------------------- #
# Estimated vs packing-stated donors
# --------------------------------------------------------------------------- #
def _mixed():
    """SN 1-4 have printed cartons (evidence), SN 5-8 do not (estimates)."""
    items = _items(8)
    packing = {1: PackingEvidence(carton_count=D("10"), matched=True),
               2: PackingEvidence(carton_count=D("20"), matched=True),
               3: PackingEvidence(carton_count=D("30"), matched=True),
               4: PackingEvidence(carton_count=D("40"), matched=True)}
    return items, packing


def test_estimates_absorb_first_and_printed_cartons_do_not_move():
    items, packing = _mixed()
    items[6].manual_package_count = D("12")
    msgs = allocate_weights_and_cartons(items, packing, D("1000"), D("200"),
                                        packing_present=True)

    _assert_lattice(items, D("200"))
    assert _ctns(items)[:4] == [D("10.00"), D("20.00"), D("30.00"), D("40.00")]
    assert "CTN_PIN_RESCALED_PACKING_EVIDENCE" not in _codes(msgs)
    assert items[0].allocation_audit["carton_source"] == "packing-list carton"


def test_printed_cartons_rescale_as_a_set_and_keep_their_ratios():
    items, packing = _mixed()
    items[6].manual_package_count = D("150")             # estimates cannot cover it
    msgs = allocate_weights_and_cartons(items, packing, D("1000"), D("200"),
                                        packing_present=True)

    _assert_lattice(items, D("200"))
    assert items[6].package_count == D("150.00")
    evid = _ctns(items)[:4]
    # printed 10 : 20 : 30 : 40 — every pairwise ratio survives to the lattice
    for k in range(1, 4):
        assert abs(evid[k] / evid[0] - (k + 1)) < D("0.01")
    assert "CTN_PIN_RESCALED_PACKING_EVIDENCE" in _codes(msgs)
    assert "CTN_DONORS_ON_FLOOR" in _codes(msgs)
    assert "scaled" in items[0].allocation_audit["carton_source"]
    assert not _blocking(msgs)


def test_all_printed_shipment_rescales_proportionally_with_no_top_n():
    items = _items(4)
    packing = {i: PackingEvidence(carton_count=D("25"), matched=True) for i in range(1, 5)}
    items[0].manual_package_count = D("40")
    msgs = allocate_weights_and_cartons(items, packing, D("1000"), D("100"),
                                        packing_present=True)

    _assert_lattice(items, D("100"))
    assert items[0].package_count == D("40.00")
    assert _ctns(items)[1:] == [D("20.00")] * 3          # 75 shared evenly, ratios intact
    assert "CTN_PIN_RESCALED_PACKING_EVIDENCE" in _codes(msgs)


def test_pin_contradicting_a_printed_carton_is_accepted_with_a_warning():
    items, packing = _mixed()
    items[0].manual_package_count = D("15")              # packing list prints 10
    msgs = allocate_weights_and_cartons(items, packing, D("1000"), D("200"),
                                        packing_present=True)

    assert items[0].package_count == D("15.00")          # the human value stands
    override = [m for m in msgs if m.code == "REVIEWED_CTN_OVERRIDES_PACKING"]
    assert len(override) == 1 and override[0].item_sequence == 1
    assert "10.00" in override[0].message and "15.00" in override[0].message
    assert not _blocking(msgs)


# --------------------------------------------------------------------------- #
# Where the pins cannot fit: the AUTHORITY gives way, never the human value
# --------------------------------------------------------------------------- #
def test_pins_leaving_no_room_block_and_name_the_needed_total():
    items = _items(3)
    items[0].manual_package_count = D("9.99")            # leaves 0.01 for two items
    msgs = allocate_weights_and_cartons(items, {}, D("1000"), D("10"), packing_present=False)

    blocking = _blocking(msgs)
    assert any(m.code == "REVIEWED_CTN_EXCEEDS_AUTHORITY" for m in blocking)
    assert any(REVIEWED_CTN_EXCEEDS_MSG in m.message for m in blocking)
    assert items[0].package_count == D("9.99")           # the pin is never clamped
    _assert_lattice(items)                               # every row still on the lattice
    hit = next(m for m in blocking if m.code == "REVIEWED_CTN_EXCEEDS_AUTHORITY")
    assert "10.01" in (hit.remediation or "")            # 9.99 + 2 x 0.01


def test_all_pinned_must_equal_the_authority():
    items = _items(3)
    for it, v in zip(items, ["2", "3", "9"]):            # 14 != 10
        it.manual_package_count = D(v)
    msgs = allocate_weights_and_cartons(items, {}, D("1000"), D("10"), packing_present=False)
    hit = next(m for m in _blocking(msgs) if m.code == "REVIEWED_CTN_TOTAL_MISMATCH")
    assert REVIEWED_CTN_MISMATCH_MSG in hit.message
    assert "14.00" in (hit.remediation or "")
    assert _ctns(items) == [D("2.00"), D("3.00"), D("9.00")]

    ok = _items(3)
    for it, v in zip(ok, ["2", "3", "5"]):               # exact -> accepted
        it.manual_package_count = D(v)
    msgs2 = allocate_weights_and_cartons(ok, {}, D("1000"), D("10"), packing_present=False)
    assert not _blocking(msgs2)
    _assert_lattice(ok, D("10"))
    assert "ITEM_CARTONS_REVIEWED" in _codes(msgs2)


def test_gross_pin_conflict_also_names_the_needed_total():
    items = _items(3)
    items[0].manual_gross_weight_kg = D("80")
    items[1].manual_gross_weight_kg = D("70")            # 150 > 100 authorised
    msgs = allocate_weights_and_cartons(items, {}, D("100"), D("10"), packing_present=False)
    hit = next(m for m in _blocking(msgs) if m.code == "REVIEWED_GROSS_EXCEEDS_AUTHORITY")
    assert "150.001" in (hit.remediation or "")          # pins + one epsilon for SN 3


def test_the_proposed_authority_excludes_nets_the_engine_would_drop():
    """Step 3b's description-net rejection is skipped on the pins-overrun
    branch, so `fixed` still holds parser guesses. Counting one would propose
    an authority the shipment does not need — and applying it would resurrect
    the very value the rejection exists to discard."""
    pinned = _item(1, "ALPHA")
    pinned.manual_gross_weight_kg = D("100")
    parsed = _item(2, "PAINT DRUM NET 900 KG")           # HIGH-confidence 900 kg parse
    msgs = allocate_weights_and_cartons([pinned, parsed], {}, D("50"), D("10"),
                                        packing_present=False)
    hit = next(m for m in _blocking(msgs) if m.code == "REVIEWED_GROSS_EXCEEDS_AUTHORITY")
    assert hit.remediation == "Set the shipment gross weight to at least 100.001 kg."

    # ...and the proposal actually works, which is the whole point of offering it
    a, b = _item(1, "ALPHA"), _item(2, "PAINT DRUM NET 900 KG")
    a.manual_gross_weight_kg = D("100")
    assert not _blocking(allocate_weights_and_cartons([a, b], {}, D("100.001"), D("10"),
                                                      packing_present=False))

    # a STATED net is still counted — only the droppable parser guess is excluded
    c, d = _item(1, "ALPHA"), _item(2, "BETA")
    c.manual_gross_weight_kg = D("100")
    d.item_weight_kg, d.item_weight_scope = D("900"), "LINE_TOTAL"
    msgs2 = allocate_weights_and_cartons([c, d], {}, D("50"), D("10"), packing_present=False)
    hit2 = next(m for m in _blocking(msgs2) if m.code == "REVIEWED_GROSS_EXCEEDS_AUTHORITY")
    assert "1000.001" in (hit2.remediation or "")


def test_no_package_authority_keeps_the_pin_and_reconciles_nothing():
    items = _items(3)
    items[1].manual_package_count = D("4")
    msgs = allocate_weights_and_cartons(items, {}, D("100"), D("0"), packing_present=False)
    assert _ctns(items) == [D("0"), D("4.00"), D("0")]   # 0 = "no packages declared"
    assert "PACKAGES_MISSING" in _codes(msgs)
    assert "CARTON_LATTICE_VIOLATION" not in _codes(msgs)


def test_no_pins_path_is_unchanged():
    items = _items(4)
    msgs = allocate_weights_and_cartons(items, {}, D("100"), D("10"), packing_present=False)
    _assert_lattice(items, D("10"))
    assert "ITEM_CARTONS_REVIEWED" not in _codes(msgs)
    assert not _blocking(msgs)


# --------------------------------------------------------------------------- #
# API: edit channel, validation, un-pinning, order independence
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def _reviewed_job(client):
    """Seeding a demo job leaves its documents extracting, so the first
    critical-review can legitimately answer 409 EXTRACTION_IN_PROGRESS — the
    review must not be computed as if a document that is still arriving did not
    exist.  Wait for it rather than racing it, the same way the UI polls."""
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    r = None
    for _ in range(80):
        r = client.get(f"/api/jobs/{job_id}/critical-review")
        if r.status_code != 409:
            break
        time.sleep(0.25)
    assert r is not None and r.status_code == 200, r.text if r is not None else "no response"
    return job_id, r.json()


def _edit(client, job_id, item_id, **fields):
    return client.patch(f"/api/jobs/{job_id}/items/{item_id}", json={"fields": fields})


def _ctn_column(review):
    return {x["item_id"]: x["ctn"] for x in review["item_details"]}


def test_carton_edit_flows_into_the_review_preview(client):
    job_id, review = _reviewed_job(client)
    row = review["item_details"][0]
    total = sum(D(x["ctn"]) for x in review["item_details"])

    r = _edit(client, job_id, row["item_id"], carton_count="3.5")
    assert r.status_code == 200
    cr = r.json()["critical_review"]
    edited = next(x for x in cr["item_details"] if x["item_id"] == row["item_id"])
    assert edited["ctn"] == "3.5" and edited["ctn_pinned"] is True
    assert sum(D(x["ctn"]) for x in cr["item_details"]) == total     # authority held
    for x in cr["item_details"]:                                     # MUST rule
        assert D(x["ctn"]) >= _MIN and D(x["ctn"]) % _MIN == 0
    assert any(w["code"] == "ITEM_CARTONS_REVIEWED" for w in cr["warnings"])


def test_carton_edit_validation(client):
    job_id, review = _reviewed_job(client)
    item_id = review["item_details"][0]["item_id"]

    r = _edit(client, job_id, item_id, carton_count="3.333")
    assert r.status_code == 422 and r.json()["code"] == "CARTON_COUNT_OFF_LATTICE"
    r = _edit(client, job_id, item_id, carton_count="0.005")
    assert r.status_code == 422 and r.json()["code"] == "CARTON_COUNT_INVALID"
    r = _edit(client, job_id, item_id, carton_count="-2")
    assert r.status_code == 422 and r.json()["code"] == "CARTON_COUNT_INVALID"
    r = _edit(client, job_id, item_id, carton_count="abc")
    assert r.status_code == 422 and r.json()["code"] == "CARTON_COUNT_INVALID"


def test_total_packages_off_the_lattice_is_rejected(client):
    job_id, _ = _reviewed_job(client)
    r = client.post(f"/api/jobs/{job_id}/shipment-totals",
                    json={"gross_weight": "199", "total_packages": "12.345"})
    assert r.status_code == 422 and r.json()["code"] == "TOTAL_PACKAGES_OFF_LATTICE"
    r = client.post(f"/api/jobs/{job_id}/shipment-totals",
                    json={"gross_weight": "199", "total_packages": "12.34"})
    assert r.status_code == 200, r.text


def test_empty_value_un_pins_and_the_computed_value_returns(client):
    job_id, review = _reviewed_job(client)
    row = review["item_details"][0]
    before = row["ctn"]

    assert _edit(client, job_id, row["item_id"], carton_count="7").status_code == 200
    cleared = _edit(client, job_id, row["item_id"], carton_count="")
    assert cleared.status_code == 200
    back = next(x for x in cleared.json()["critical_review"]["item_details"]
                if x["item_id"] == row["item_id"])
    assert back["ctn_pinned"] is False and back["ctn"] == before

    # the same gesture works on the weight pins
    assert _edit(client, job_id, row["item_id"], gross_weight="12").status_code == 200
    r = _edit(client, job_id, row["item_id"], gross_weight="")
    assert r.status_code == 200
    row2 = next(x for x in r.json()["critical_review"]["item_details"]
                if x["item_id"] == row["item_id"])
    assert row2["gross_pinned"] is False


def test_two_pins_are_order_independent(client):
    """The overlay is replayed from scratch on every recompute, so the outcome
    must depend on the SET of pins and not the order they were entered — two
    reviewers making the same edits must get the same declaration."""
    job_a, review = _reviewed_job(client)
    id1 = review["item_details"][0]["item_id"]
    id2 = review["item_details"][5]["item_id"]
    _edit(client, job_a, id1, carton_count="4")
    a = _edit(client, job_a, id2, carton_count="6").json()["critical_review"]

    job_b, review_b = _reviewed_job(client)
    id1b = review_b["item_details"][0]["item_id"]
    id2b = review_b["item_details"][5]["item_id"]
    _edit(client, job_b, id2b, carton_count="6")
    b = _edit(client, job_b, id1b, carton_count="4").json()["critical_review"]

    # compare by item_id, and say so plainly if the two seeded jobs are not the
    # same shipment — a CTN mismatch would otherwise read as a rule failure
    assert ([x["item_id"] for x in a["item_details"]]
            == [x["item_id"] for x in b["item_details"]]), "the two demo jobs differ"
    assert _ctn_column(a) == _ctn_column(b)


def test_pins_reset_when_packing_evidence_changes():
    """A pin was made against the packing/AWB evidence that just changed and is
    reconciled against the authority it sets, so it is discarded — while the
    invoice-side edits on the same item survive, because the invoice did not
    change (user rule 2026-08-02)."""
    from app.review.item_mutations import reset_for_evidence_change

    overlay = {"schema": 1, "revision": 3, "ordered_item_ids": [], "manual_items": [],
               "tombstones": [], "hs_selections": {}, "shipment_override": None,
               "field_edits": {"src:a": {"description": "WIDGET", "gross_weight": "5",
                                         "carton_count": "3", "edited_at": "x"},
                               "src:b": {"carton_count": "9", "edited_at": "x"}},
               "coo_all": None, "bms_edits": {}, "reset_notice": None}

    new, fold, discarded = reset_for_evidence_change(overlay, "PACKING_LIST", "re-extracted")
    assert discarded["weight_carton_pins"] == 2
    assert new["field_edits"]["src:a"] == {"description": "WIDGET", "edited_at": "x"}
    assert "src:b" not in new["field_edits"]             # nothing but pins -> gone

    # an INVOICE change still clears the whole item channel, pins included
    inv, _, inv_discarded = reset_for_evidence_change(overlay, "INVOICE", "re-extracted")
    assert inv["field_edits"] == {} and inv_discarded["field_edits"] == 2
