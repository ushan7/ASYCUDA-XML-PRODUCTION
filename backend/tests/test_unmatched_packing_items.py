"""An item the packing list never mentions must not be declared at 1 gram (A3).

Conditions 1 and 2 were all-or-nothing: `have_gross` was a single flag over the
WHOLE shipment, so one item with a packing gross put every item on that basis
and an item the packing list did not mention got a basis of ZERO.  Zero is not
"no information" to an apportionment — it is "no weight".  The item fell to the
`_EPS` floor and was declared at:

    gross 0.001 kg   net 0.000 kg   validation_status OK   warnings: none

with the shipment totals reconciling exactly, `net < gross` satisfied, and the
declaration validator passing it.  Three equal invoice lines, one of them
matched, produced 299.998 / 0.001 / 0.001 against a 300 kg authority.  Nothing
anywhere said a match had been missed — not the audit trail, not the review
screen, not the XML.

An unmatched item now takes its fallback share (invoice value, or quantity when
no packing list was uploaded) scaled by what the MATCHED items actually weigh
per unit of that share, and every unmatched item is named in a warning.  The
estimate is not claimed to be evidence: the audit trail says "estimated", and
the reviewer is told to verify or pin the weights.
"""
from decimal import Decimal

from app.rules.models import WorkItem
from app.rules.packing_match import PackingEvidence, match_packing
from app.rules.weight_carton import allocate_weights_and_cartons
from app.extraction.common_models import PackingListChunkRaw


def _item(sn, desc, qty, total):
    return WorkItem(
        xml_item_sequence=sn, source_invoice_number="INV-1", source_invoice_date="",
        source_invoice_item_index=sn, source_invoice_item_no=str(sn),
        description_raw=desc, quantity=Decimal(qty), invoice_uom_raw="PCS",
        unit_price=Decimal(total) / Decimal(qty), line_total=Decimal(total),
        currency="USD")


def _codes(msgs):
    return {m.code for m in msgs}


# --------------------------------------------------------------------------- #
# The regression itself
# --------------------------------------------------------------------------- #
def test_unmatched_items_are_not_collapsed_to_the_floor():
    items = [_item(1, "WIDGET A", "10", "1000"), _item(2, "WIDGET B", "10", "1000"),
             _item(3, "WIDGET C", "10", "1000")]
    packing = {1: PackingEvidence(gross_weight=Decimal("30"), matched=True,
                                  matched_name="WIDGET A")}
    msgs = allocate_weights_and_cartons(items, packing, Decimal("300"), Decimal("12"))

    # equal invoice values -> equal weights, NOT 299.998 / 0.001 / 0.001
    assert [i.gross_weight_kg for i in items] == [Decimal("100.000")] * 3
    assert all(i.gross_weight_kg > Decimal("1") for i in items)
    # the exact-sum invariants still hold
    assert sum(i.gross_weight_kg for i in items) == Decimal("300.000")
    assert sum(i.package_count for i in items) == Decimal("12.00")
    assert all(i.net_weight_kg < i.gross_weight_kg for i in items)
    assert "PACKING_ITEMS_UNMATCHED" in _codes(msgs)


def test_the_warning_names_every_unmatched_sn_and_says_it_is_estimated():
    items = [_item(1, "A", "1", "100"), _item(2, "B", "1", "100"), _item(3, "C", "1", "100")]
    packing = {2: PackingEvidence(gross_weight=Decimal("5"), matched=True, matched_name="B")}
    msgs = allocate_weights_and_cartons(items, packing, Decimal("15"), Decimal("3"))

    w = next(m for m in msgs if m.code == "PACKING_ITEMS_UNMATCHED")
    assert "SN 1, 3" in w.message
    assert "2 of 3" in w.message
    assert "ESTIMATED" in w.message


def test_audit_trail_distinguishes_estimated_from_read():
    items = [_item(1, "A", "1", "100"), _item(2, "B", "1", "100")]
    packing = {1: PackingEvidence(gross_weight=Decimal("8"), matched=True, matched_name="A")}
    allocate_weights_and_cartons(items, packing, Decimal("16"), Decimal("2"))

    assert items[0].allocation_audit["gross_weight_source"] == "packing-list gross weight"
    assert "estimated" in items[1].allocation_audit["gross_weight_source"]
    assert "no packing-list match" in items[1].allocation_audit["gross_weight_source"]


# --------------------------------------------------------------------------- #
# The estimate follows the matched items' density, not a flat share
# --------------------------------------------------------------------------- #
def test_estimate_scales_with_what_the_matched_items_weigh_per_unit_of_value():
    """SN2 is worth 3x SN1, so it is estimated at 3x SN1's weight."""
    items = [_item(1, "A", "1", "100"), _item(2, "B", "1", "300")]
    packing = {1: PackingEvidence(gross_weight=Decimal("10"), matched=True, matched_name="A")}
    allocate_weights_and_cartons(items, packing, Decimal("40"), Decimal("4"))

    assert items[1].gross_weight_kg == items[0].gross_weight_kg * 3
    assert sum(i.gross_weight_kg for i in items) == Decimal("40.000")


def test_quantity_share_is_the_fallback_when_no_packing_list_was_uploaded():
    items = [_item(1, "A", "1", "500"), _item(2, "B", "9", "500")]
    packing = {1: PackingEvidence(gross_weight=Decimal("10"), matched=True, matched_name="A")}
    msgs = allocate_weights_and_cartons(items, packing, Decimal("100"), Decimal("4"),
                                        packing_present=False)
    # quantity 1 : 9 — the equal VALUES must not be what drives the estimate
    assert items[1].gross_weight_kg == items[0].gross_weight_kg * 9
    assert "item quantity" in next(m for m in msgs
                                   if m.code == "PACKING_ITEMS_UNMATCHED").message


def test_condition_2_cartons_only_estimates_unmatched_items_too():
    items = [_item(1, "A", "1", "100"), _item(2, "B", "1", "100")]
    packing = {1: PackingEvidence(carton_count=Decimal("2"), matched=True, matched_name="A")}
    msgs = allocate_weights_and_cartons(items, packing, Decimal("50"), Decimal("4"))

    assert items[1].gross_weight_kg == items[0].gross_weight_kg      # equal value
    assert all(i.gross_weight_kg > Decimal("1") for i in items)
    assert "PACKING_ITEMS_UNMATCHED" in _codes(msgs)


def test_matched_rows_with_no_usable_value_fall_back_to_the_matched_mean():
    """Nothing to scale by: every matched item is priced 0, so an unmatched item
    is estimated at the matched mean rather than dividing by zero."""
    items = [_item(1, "A", "1", "0"), _item(2, "B", "1", "0"), _item(3, "C", "1", "0")]
    packing = {1: PackingEvidence(gross_weight=Decimal("6"), matched=True, matched_name="A")}
    msgs = allocate_weights_and_cartons(items, packing, Decimal("30"), Decimal("3"))

    assert sum(i.gross_weight_kg for i in items) == Decimal("30.000")
    assert all(i.gross_weight_kg > Decimal("1") for i in items)
    assert "PACKING_ITEMS_UNMATCHED" in _codes(msgs)


# --------------------------------------------------------------------------- #
# No false alarms on the healthy path
# --------------------------------------------------------------------------- #
def test_fully_matched_shipment_is_unchanged_and_silent():
    items = [_item(1, "A", "1", "100"), _item(2, "B", "1", "100")]
    packing = {1: PackingEvidence(gross_weight=Decimal("30"), matched=True, matched_name="A"),
               2: PackingEvidence(gross_weight=Decimal("70"), matched=True, matched_name="B")}
    msgs = allocate_weights_and_cartons(items, packing, Decimal("100"), Decimal("4"))

    # packing shape preserved exactly (30:70), not flattened by the change
    assert [i.gross_weight_kg for i in items] == [Decimal("30.000"), Decimal("70.000")]
    assert "PACKING_ITEMS_UNMATCHED" not in _codes(msgs)


def test_no_packing_evidence_at_all_keeps_the_documented_fallbacks():
    items = [_item(1, "A", "3", "500"), _item(2, "B", "1", "500")]
    value = allocate_weights_and_cartons(items, {}, Decimal("100"), Decimal("4"),
                                         packing_present=True)
    assert [i.gross_weight_kg for i in items] == [Decimal("50.000"), Decimal("50.000")]
    assert "WEIGHT_BASIS_VALUE" in _codes(value)
    assert "PACKING_ITEMS_UNMATCHED" not in _codes(value)   # nothing to match against

    items = [_item(1, "A", "3", "500"), _item(2, "B", "1", "500")]
    qty = allocate_weights_and_cartons(items, {}, Decimal("100"), Decimal("4"),
                                       packing_present=False)
    assert [i.gross_weight_kg for i in items] == [Decimal("75.000"), Decimal("25.000")]
    assert "WEIGHT_BASIS_QUANTITY" in _codes(qty)


# --------------------------------------------------------------------------- #
# The packing side: rows that match no invoice item
# --------------------------------------------------------------------------- #
def test_unused_packing_rows_are_reported():
    items = [_item(1, "WIDGET A", "1", "100")]
    payload = PackingListChunkRaw.model_validate({
        "role_validation": {"expected_role": "PACKING_LIST", "matches_expected_role": True},
        "rows": [
            {"source_page_no": 1, "source_row_index": 1, "description_raw": "WIDGET A",
             "gross_weight": {"value_raw": "10", "unit_raw": "KG"}},
            {"source_page_no": 1, "source_row_index": 2, "description_raw": "GADGET Z",
             "gross_weight": {"value_raw": "40", "unit_raw": "KG"}},
        ],
    })
    warnings = []
    match_packing(items, [payload], warnings_out=warnings)

    w = next(m for m in warnings if m.code == "PACKING_ROWS_UNMATCHED")
    assert "GADGET Z" in w.message
    assert "1 of 2" in w.message


def test_fully_matched_packing_list_reports_nothing():
    items = [_item(1, "WIDGET A", "1", "100")]
    payload = PackingListChunkRaw.model_validate({
        "role_validation": {"expected_role": "PACKING_LIST", "matches_expected_role": True},
        "rows": [{"source_page_no": 1, "source_row_index": 1, "description_raw": "WIDGET A",
                  "gross_weight": {"value_raw": "10", "unit_raw": "KG"}}],
    })
    warnings = []
    match_packing(items, [payload], warnings_out=warnings)
    assert "PACKING_ROWS_UNMATCHED" not in _codes(warnings)


# --------------------------------------------------------------------------- #
# A gap that only exists at 3dp is not a gap a customs officer can see
# --------------------------------------------------------------------------- #
def test_invisible_net_gross_gap_is_flagged():
    items = [_item(i, f"ITEM {i}", "1", "100") for i in range(1, 4)]
    # 0.03 kg total: every item lands at 0.010 gross / 0.007 net — distinct at
    # 3dp, identical at the 2dp printed on the declaration
    msgs = allocate_weights_and_cartons(items, {}, Decimal("0.03"), Decimal("3"))

    assert all(i.net_weight_kg < i.gross_weight_kg for i in items)      # still valid
    flagged = [w for it in items for w in it.warnings if w.code == "WEIGHT_GAP_INVISIBLE"]
    assert len(flagged) == 3
    assert not [m for m in msgs if m.severity.value == "BLOCKING"]


def test_normal_weights_do_not_trip_the_visible_gap_check():
    items = [_item(1, "A", "1", "100"), _item(2, "B", "1", "100")]
    allocate_weights_and_cartons(items, {}, Decimal("100"), Decimal("4"))
    assert not [w for it in items for w in it.warnings if w.code == "WEIGHT_GAP_INVISIBLE"]
