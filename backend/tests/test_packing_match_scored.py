"""Matching a packing list to invoice items: the ladder below exact equality.

Matching used to be exact normalized string equality with no fallback and no
confidence.  Two people typing the same goods on two documents is enough to
lose the match entirely — and a lost match is invisible: the item silently
falls to an ESTIMATED weight rather than the one the packing list printed.

The ladder is exact name -> product code -> scored similarity, and every rung
below the first reports itself to the reviewer.  What it must never do is merge
two sizes or variants of one product, so a similarity match is gated on the
measurements agreeing and on the winner being clearly ahead of the runner-up.
"""
from decimal import Decimal

from app.extraction.common_models import PackingListChunkRaw
from app.rules.models import WorkItem
from app.rules.packing_match import match_packing

D = Decimal


def _item(seq, desc, qty="1", total="100", model=None):
    return WorkItem(
        xml_item_sequence=seq, source_invoice_number="INV-1", source_invoice_date="",
        source_invoice_item_index=seq, source_invoice_item_no=None,
        description_raw=desc, quantity=D(qty), invoice_uom_raw="PCS",
        unit_price=D("1"), line_total=D(total), currency="USD", model_raw=model)


def _packing(rows):
    return PackingListChunkRaw.model_validate({
        "role_validation": {"expected_role": "PACKING_LIST", "matches_expected_role": True},
        "rows": [{"source_page_no": 1, "source_row_index": i + 1, **r} for i, r in enumerate(rows)],
    })


def _match(items, rows):
    warnings = []
    ev = match_packing(items, [_packing(rows)], warnings_out=warnings)
    return ev, {w.code for w in warnings}, warnings


def test_exact_name_match_is_full_confidence_and_silent():
    items = [_item(1, "SHAMPOO 500 ML")]
    ev, codes, _ = _match(items, [{"description_raw": "Shampoo  500ml",
                                   "gross_weight": {"value_raw": "7.5", "unit_raw": "KG"}}])
    assert ev[1].gross_weight == D("7.5")
    assert ev[1].match_confidence == D("1") and ev[1].match_method == "exact description"
    assert "PACKING_MATCH_LOW_CONFIDENCE" not in codes


def test_reworded_description_matches_by_similarity_and_is_reported():
    """'500ML SHAMPOO BOTTLE' vs 'Shampoo Bottle 500 ml' — same goods, different
    typist.  Exact equality alone threw this weight away."""
    items = [_item(1, "Shampoo Bottle 500 ml")]
    ev, codes, msgs = _match(items, [{"description_raw": "500ML SHAMPOO BOTTLE",
                                      "gross_weight": {"value_raw": "7.5", "unit_raw": "KG"}}])
    assert ev[1].gross_weight == D("7.5")
    assert ev[1].match_method == "description similarity"
    assert D("0.6") <= ev[1].match_confidence < D("1")
    assert "PACKING_MATCH_LOW_CONFIDENCE" in codes
    assert "SN 1" in next(m.message for m in msgs if m.code == "PACKING_MATCH_LOW_CONFIDENCE")


def test_different_size_never_matches_however_similar_the_words():
    """The spec forbids merging items whose size/variant differs.  Every word
    here agrees; only the measurement does not, and that is decisive."""
    items = [_item(1, "SHAMPOO BOTTLE 250 ML")]
    ev, codes, _ = _match(items, [{"description_raw": "SHAMPOO BOTTLE 500 ML",
                                   "gross_weight": {"value_raw": "7.5", "unit_raw": "KG"}}])
    assert ev[1].gross_weight is None and not ev[1].matched
    assert "PACKING_ROWS_UNMATCHED" in codes


def test_equally_similar_candidates_are_left_unmatched_not_guessed():
    items = [_item(1, "WIDGET ALPHA GRADE"), _item(2, "WIDGET BETA GRADE")]
    ev, codes, _ = _match(items, [{"description_raw": "WIDGET GRADE",
                                   "gross_weight": {"value_raw": "7.5", "unit_raw": "KG"}}])
    assert not ev[1].matched and not ev[2].matched
    assert "PACKING_MATCH_AMBIGUOUS" in codes


def test_the_best_match_wins_regardless_of_packing_row_order():
    """Two packing rows resemble one invoice item; the SECOND is far closer.
    Assigning in row order would hand the item to the weaker one and leave the
    better match unmatched — the pairing would depend on the order the supplier
    typed their packing list, which is what 'never by row' rules out."""
    items = [_item(1, "SHAMPOO BOTTLE 500 ML")]
    ev, _, _ = _match(items, [
        {"description_raw": "SHAMPOO BOTTLE CLEAR PLASTIC 500 ML CAP",
         "gross_weight": {"value_raw": "1", "unit_raw": "KG"}},
        {"description_raw": "BOTTLE SHAMPOO 500 ML",
         "gross_weight": {"value_raw": "9", "unit_raw": "KG"}},
    ])
    assert ev[1].gross_weight == D("9")            # the closer wording won


def test_product_code_outranks_wording_differences():
    items = [_item(1, "CORONARY STENT SYSTEM 2.25X15", model="RONYX22515X")]
    ev, codes, _ = _match(items, [{"description_raw": "STENT ONYX FRONTIER RX",
                                   "item_code_raw": "RONYX22515X",
                                   "gross_weight": {"value_raw": "3.2", "unit_raw": "KG"}}])
    assert ev[1].gross_weight == D("3.2")
    assert ev[1].match_method == "product code" and ev[1].match_confidence == D("0.95")


def test_one_invoice_line_is_never_claimed_by_two_packing_products():
    """An exact match owns its invoice line; a later similarity match must look
    elsewhere rather than double-book it."""
    items = [_item(1, "WIDGET ALPHA GRADE")]
    ev, codes, _ = _match(items, [
        {"description_raw": "WIDGET ALPHA GRADE", "gross_weight": {"value_raw": "5", "unit_raw": "KG"}},
        {"description_raw": "WIDGET ALPHA GRADE XL", "gross_weight": {"value_raw": "9", "unit_raw": "KG"}},
    ])
    assert ev[1].gross_weight == D("5") and ev[1].match_confidence == D("1")
    assert "PACKING_ROWS_UNMATCHED" in codes


# --------------------------------------------------------------------------- #
# Shared cartons — a repeated value is ambiguous; a carton RANGE is not
# --------------------------------------------------------------------------- #
def test_range_group_id_settles_the_repeated_value_ambiguity():
    """Three rows in 'cartons 1-5', each printing 1 carton.  Read as a repeated
    group total that is 1 carton for the whole group; the printed range says
    five.  The range is the physical fact."""
    items = [_item(1, "A1"), _item(2, "A2"), _item(3, "A3")]
    ev, _, _ = _match(items, [
        {"description_raw": "A1", "carton_count": {"value_raw": "1"}, "shared_carton_group_raw": "1-5"},
        {"description_raw": "A2", "carton_count": {"value_raw": "1"}, "shared_carton_group_raw": "1-5"},
        {"description_raw": "A3", "carton_count": {"value_raw": "1"}, "shared_carton_group_raw": "1-5"},
    ])
    assert ev[1].carton_count + ev[2].carton_count + ev[3].carton_count == D("5")
    assert all(ev[i].carton_shared for i in (1, 2, 3))


def test_non_range_group_id_keeps_the_repeated_total_reading():
    """Regression guard: 'G1' names no range, so the historical reading (every
    row repeats the group total) still stands."""
    items = [_item(1, "A1"), _item(2, "A2")]
    ev, _, _ = _match(items, [
        {"description_raw": "A1", "carton_count": {"value_raw": "2"}, "shared_carton_group_raw": "G1"},
        {"description_raw": "A2", "carton_count": {"value_raw": "2"}, "shared_carton_group_raw": "G1"},
    ])
    assert ev[1].carton_count + ev[2].carton_count == D("2")


def test_single_carton_group_id_is_not_treated_as_a_range():
    items = [_item(1, "A1"), _item(2, "A2")]
    ev, _, _ = _match(items, [
        {"description_raw": "A1", "carton_count": {"value_raw": "3"}, "shared_carton_group_raw": "7"},
        {"description_raw": "A2", "carton_count": {"value_raw": "3"}, "shared_carton_group_raw": "7"},
    ])
    assert ev[1].carton_count + ev[2].carton_count == D("3")
