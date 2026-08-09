"""Condition 1 and Condition 2 together, and the invoice-weight override.

Two behaviours with no previous coverage anywhere in the suite:

* a packing list that prints BOTH item gross weights and item cartons — the
  ordinary case — used to have its stated cartons thrown away, because
  `have_carton` was `(not have_gross) and …`;
* the net ladder's order between an invoice-printed weight and a description
  conversion.  No test gave an item both, and the 119-row golden invoice has
  neither, so the ladder could be reordered in either direction with the whole
  suite green.
"""
from decimal import Decimal

from app.extraction.common_models import PackingListChunkRaw
from app.rules.models import WorkItem
from app.rules.packing_match import match_packing
from app.rules.weight_carton import allocate_weights_and_cartons

D = Decimal


def _item(seq, desc, qty="1", total="100", weight=None, scope="LINE_TOTAL", uom="PCS"):
    return WorkItem(
        xml_item_sequence=seq, source_invoice_number="INV-1", source_invoice_date="",
        source_invoice_item_index=seq, source_invoice_item_no=None,
        description_raw=desc, quantity=D(qty), invoice_uom_raw=uom,
        unit_price=D("1"), line_total=D(total), currency="USD",
        item_weight_kg=D(weight) if weight else None, item_weight_scope=scope)


def _packing(rows):
    return PackingListChunkRaw.model_validate({
        "role_validation": {"expected_role": "PACKING_LIST", "matches_expected_role": True},
        "rows": [{"source_page_no": 1, "source_row_index": i + 1, **r} for i, r in enumerate(rows)],
    })


# --------------------------------------------------------------------------- #
# A4 — both conditions at once
# --------------------------------------------------------------------------- #
def test_packing_gross_and_packing_cartons_are_both_used():
    """Gross follows the printed weights, cartons follow the printed cartons.
    Neither is re-derived from the other when the document states both."""
    items = [_item(1, "ALPHA"), _item(2, "BETA")]
    packing = match_packing(items, [_packing([
        {"description_raw": "ALPHA", "gross_weight": {"value_raw": "30", "unit_raw": "KG"},
         "carton_count": {"value_raw": "1"}},
        {"description_raw": "BETA", "gross_weight": {"value_raw": "70", "unit_raw": "KG"},
         "carton_count": {"value_raw": "9"}},
    ])])
    msgs = allocate_weights_and_cartons(items, packing, D("100"), D("10"))
    assert [i.gross_weight_kg for i in items] == [D("30.0000"), D("70.0000")]
    # 1 / 9 as PRINTED — not the 3 / 7 the weights would have implied
    assert [i.package_count for i in items] == [D("1.00"), D("9.00")]
    assert items[0].allocation_audit["carton_source"] == "packing-list carton"
    assert items[0].allocation_audit["gross_weight_source"] == "packing-list gross weight"
    assert not [m for m in msgs if m.severity.value == "BLOCKING"]


def test_item_with_gross_but_no_printed_carton_is_estimated_not_floored():
    """The zero-basis trap, on the carton side: an item the packing list gives
    no carton for must take its share of the cartons, not land on the 0.01
    floor under an audit trail that says 'packing-list carton'."""
    items = [_item(1, "ALPHA"), _item(2, "BETA")]
    packing = match_packing(items, [_packing([
        {"description_raw": "ALPHA", "gross_weight": {"value_raw": "50", "unit_raw": "KG"},
         "carton_count": {"value_raw": "5"}},
        {"description_raw": "BETA", "gross_weight": {"value_raw": "50", "unit_raw": "KG"}},
    ])])
    allocate_weights_and_cartons(items, packing, D("100"), D("10"))
    assert [i.package_count for i in items] == [D("5.00"), D("5.00")]
    assert items[1].allocation_audit["carton_source"] == (
        "estimated from weight share (no packing-list carton)")


def test_cartons_still_follow_weight_when_the_packing_list_states_none():
    items = [_item(1, "ALPHA"), _item(2, "BETA")]
    packing = match_packing(items, [_packing([
        {"description_raw": "ALPHA", "gross_weight": {"value_raw": "30", "unit_raw": "KG"}},
        {"description_raw": "BETA", "gross_weight": {"value_raw": "70", "unit_raw": "KG"}},
    ])])
    allocate_weights_and_cartons(items, packing, D("100"), D("10"))
    assert [i.package_count for i in items] == [D("3.00"), D("7.00")]
    assert items[0].allocation_audit["carton_source"] == "proportional by gross weight"


def test_cartons_only_still_drives_the_gross_split():
    items = [_item(1, "ALPHA"), _item(2, "BETA")]
    packing = match_packing(items, [_packing([
        {"description_raw": "ALPHA", "carton_count": {"value_raw": "3.5"}},
        {"description_raw": "BETA", "carton_count": {"value_raw": "1.5"}},
    ])])
    allocate_weights_and_cartons(items, packing, D("100"), D("5"))
    assert [i.gross_weight_kg for i in items] == [D("70.0000"), D("30.0000")]
    assert [i.package_count for i in items] == [D("3.50"), D("1.50")]


# --------------------------------------------------------------------------- #
# A5 — invoice-printed weight is the highest non-reviewer authority
# --------------------------------------------------------------------------- #
def _codes(msgs, items):
    return {m.code for m in msgs} | {w.code for it in items for w in it.warnings}


def test_invoice_weight_outranks_a_high_confidence_description_conversion():
    """'SHAMPOO 1000 ML' converts at HIGH confidence (unambiguous unit, known
    density) to 1 kg.  The invoice prints 5 kg.  A person wrote the 5."""
    items = [_item(1, "SHAMPOO 1000 ML", weight="5"), _item(2, "FILLER")]
    msgs = allocate_weights_and_cartons(items, {}, D("100"), D("10"), packing_present=False)
    assert items[0].net_weight_kg == D("5.000")
    assert items[0].allocation_audit["net_weight_source"] == "invoice weight override"
    assert "NET_WEIGHT_SOURCES_DISAGREE" not in _codes(msgs, items)      # 5x, not 10x


def test_description_conversion_still_applies_when_the_invoice_prints_no_weight():
    items = [_item(1, "SHAMPOO 1000 ML"), _item(2, "FILLER")]
    allocate_weights_and_cartons(items, {}, D("100"), D("10"), packing_present=False)
    assert items[0].net_weight_kg == D("1.000")
    assert "description unit conversion" in items[0].allocation_audit["net_weight_source"]


def test_tenfold_disagreement_between_the_two_sources_is_reported():
    items = [_item(1, "SHAMPOO 1000 ML", weight="50"), _item(2, "FILLER")]
    msgs = allocate_weights_and_cartons(items, {}, D("100"), D("10"), packing_present=False)
    assert items[0].net_weight_kg == D("50.000")                 # the stated value still wins
    assert "NET_WEIGHT_SOURCES_DISAGREE" in _codes(msgs, items)
    note = next(w.message for w in items[0].warnings if w.code == "NET_WEIGHT_SOURCES_DISAGREE")
    assert "50" in note and "50x" in note


def test_reviewer_pin_still_outranks_the_invoice_weight():
    items = [_item(1, "SHAMPOO 1000 ML", weight="5"), _item(2, "FILLER")]
    items[0].manual_net_weight_kg = D("2")
    allocate_weights_and_cartons(items, {}, D("100"), D("10"), packing_present=False)
    assert items[0].net_weight_kg == D("2.000")
    assert items[0].allocation_audit["net_weight_source"] == "reviewer-entered net weight"


def test_invoice_weight_outranks_the_packing_list_net():
    items = [_item(1, "ALPHA", weight="5"), _item(2, "BETA")]
    packing = match_packing(items, [_packing([
        {"description_raw": "ALPHA", "gross_weight": {"value_raw": "30", "unit_raw": "KG"},
         "net_weight": {"value_raw": "20", "unit_raw": "KG"}},
        {"description_raw": "BETA", "gross_weight": {"value_raw": "70", "unit_raw": "KG"}},
    ])])
    allocate_weights_and_cartons(items, packing, D("100"), D("10"))
    assert items[0].net_weight_kg == D("5.000")
    assert items[0].allocation_audit["net_weight_source"] == "invoice weight override"
