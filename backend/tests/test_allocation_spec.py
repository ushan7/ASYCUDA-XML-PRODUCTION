"""Carton & weight distribution spec (user rules 2026-07-17).

Covers: Condition 1 (item-wise gross), Condition 2 (cartons only), duplicate
grouping, shared cartons, multi-invoice-line distribution, Override 1 (invoice
weight), Override 2 (description unit conversion), final reconciliation with
exact sums and the two impossibility errors, the packing extraction time
budget fallback, and the derived packing view.
"""
from decimal import Decimal
from types import SimpleNamespace

from app.extraction.common_models import PackingListChunkRaw
from app.rules.description_weight import net_from_description
from app.rules.models import WorkItem
from app.rules.packing_match import match_packing
from app.rules.weight_carton import (
    CARTON_TOO_SMALL_MSG,
    GROSS_NOT_ABOVE_NET_MSG,
    allocate_weights_and_cartons,
)

D = Decimal


def _item(seq, desc, qty="1", total="100", weight=None, scope="UNKNOWN", uom="PCS"):
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
# CONDITION 1 — packing has item-wise gross weight
# --------------------------------------------------------------------------- #
def test_condition1_gross_basis_cartons_by_weight_and_exact_sums():
    items = [_item(1, "ALPHA"), _item(2, "BETA")]
    packing = match_packing(items, [_packing([
        {"description_raw": "ALPHA", "gross_weight": {"value_raw": "30"},
         "net_weight": {"value_raw": "20"}},
        {"description_raw": "BETA", "gross_weight": {"value_raw": "70"}},
    ])])
    msgs = allocate_weights_and_cartons(items, packing, D("100"), D("10"))
    assert [i.gross_weight_kg for i in items] == [D("30.0000"), D("70.0000")]
    assert [i.package_count for i in items] == [D("3.00"), D("7.00")]      # cartons ∝ gross
    assert items[0].net_weight_kg == D("20.0000")                          # packing net used
    assert items[1].net_weight_kg == D("49.000")                           # 0.7 x FINAL gross
    assert sum(i.gross_weight_kg for i in items) == D("100.0000")
    assert items[0].allocation_audit["net_weight_source"] == "packing-list net weight"
    assert "0.7" in items[1].allocation_audit["net_weight_source"]
    assert not [m for m in msgs if m.severity.value == "BLOCKING"]


def test_condition1_duplicate_packing_rows_are_summed_first():
    items = [_item(1, "ALPHA")]
    packing = match_packing(items, [_packing([
        {"description_raw": "ALPHA", "gross_weight": {"value_raw": "10"}},
        {"description_raw": "Alpha ", "gross_weight": {"value_raw": "15"}},   # same normalized item
    ])])
    assert packing[1].gross_weight == D("25")


def test_same_item_on_multiple_invoice_lines_split_by_quantity_then_value():
    # qty split 3:1
    items = [_item(1, "ALPHA", qty="3"), _item(2, "ALPHA", qty="1")]
    ev = match_packing(items, [_packing([
        {"description_raw": "ALPHA", "gross_weight": {"value_raw": "40"}}])])
    assert (ev[1].gross_weight, ev[2].gross_weight) == (D("30"), D("10"))
    # zero quantities -> value split 1:3
    items = [_item(1, "ALPHA", qty="0", total="25"), _item(2, "ALPHA", qty="0", total="75")]
    ev = match_packing(items, [_packing([
        {"description_raw": "ALPHA", "gross_weight": {"value_raw": "40"}}])])
    assert (ev[1].gross_weight, ev[2].gross_weight) == (D("10"), D("30"))
    # zero everything -> equal split
    items = [_item(1, "ALPHA", qty="0", total="0"), _item(2, "ALPHA", qty="0", total="0")]
    ev = match_packing(items, [_packing([
        {"description_raw": "ALPHA", "gross_weight": {"value_raw": "40"}}])])
    assert (ev[1].gross_weight, ev[2].gross_weight) == (D("20"), D("20"))


# --------------------------------------------------------------------------- #
# CONDITION 2 — packing has cartons only (+ shared cartons)
# --------------------------------------------------------------------------- #
def test_condition2_cartons_drive_gross_distribution():
    items = [_item(1, "ALPHA"), _item(2, "BETA")]
    packing = match_packing(items, [_packing([
        {"description_raw": "ALPHA", "carton_count": {"value_raw": "1"}},
        {"description_raw": "ALPHA", "carton_count": {"value_raw": "2"}},
        {"description_raw": "ALPHA", "carton_count": {"value_raw": "0.5"}},   # grouped: 3.5
        {"description_raw": "BETA", "carton_count": {"value_raw": "1.5"}},
    ])])
    assert packing[1].carton_count == D("3.5")
    msgs = allocate_weights_and_cartons(items, packing, D("100"), D("5"))
    assert [i.package_count for i in items] == [D("3.50"), D("1.50")]
    # gross ∝ carton share: 70 / 30
    assert [i.gross_weight_kg for i in items] == [D("70.0000"), D("30.0000")]
    assert sum(i.package_count for i in items) == D("5.00")
    assert items[0].allocation_audit["carton_source"] == "packing-list carton"
    assert not [m for m in msgs if m.severity.value == "BLOCKING"]


def test_shared_carton_group_counted_once_and_divided():
    # three items share 2 cartons; every row repeats the group total (2)
    items = [_item(1, "A1", qty="1"), _item(2, "A2", qty="1"), _item(3, "A3", qty="2")]
    ev = match_packing(items, [_packing([
        {"description_raw": "A1", "quantity_raw": "1",
         "carton_count": {"value_raw": "2"}, "shared_carton_group_raw": "G1"},
        {"description_raw": "A2", "quantity_raw": "1",
         "carton_count": {"value_raw": "2"}, "shared_carton_group_raw": "G1"},
        {"description_raw": "A3", "quantity_raw": "2",
         "carton_count": {"value_raw": "2"}, "shared_carton_group_raw": "G1"},
    ])])
    total = ev[1].carton_count + ev[2].carton_count + ev[3].carton_count
    assert total == D("2")                                   # never duplicated
    assert ev[3].carton_count == D("1")                      # qty-weighted share (2 of 4)
    assert ev[1].carton_shared and ev[3].carton_shared


def test_shared_carton_rows_with_presplit_fractions_are_summed():
    items = [_item(1, "A1"), _item(2, "A2"), _item(3, "A3")]
    ev = match_packing(items, [_packing([
        {"description_raw": "A1", "carton_count": {"value_raw": "0.6"}, "shared_carton_group_raw": "G1"},
        {"description_raw": "A2", "carton_count": {"value_raw": "0.6"}, "shared_carton_group_raw": "G1"},
        {"description_raw": "A3", "carton_count": {"value_raw": "0.8"}, "shared_carton_group_raw": "G1"},
    ])])
    total = ev[1].carton_count + ev[2].carton_count + ev[3].carton_count
    assert total == D("2.0")                                 # 0.6+0.6+0.8 kept as printed


# --------------------------------------------------------------------------- #
# Overrides
# --------------------------------------------------------------------------- #
def test_override1_invoice_weight_is_net_priority():
    items = [_item(1, "ALPHA", qty="4", weight="2", scope="PER_UNIT"),   # 8 kg net
             _item(2, "BETA", weight="5", scope="LINE_TOTAL")]          # 5 kg net
    packing = match_packing(items, [_packing([
        {"description_raw": "ALPHA", "gross_weight": {"value_raw": "50"},
         "net_weight": {"value_raw": "40"}},                            # must be overridden
        {"description_raw": "BETA", "gross_weight": {"value_raw": "50"}},
    ])])
    allocate_weights_and_cartons(items, packing, D("100"), D("2"))
    assert items[0].net_weight_kg == D("8.0000")
    assert items[1].net_weight_kg == D("5.0000")
    assert items[0].allocation_audit["net_weight_source"] == "invoice weight override"
    assert "1.2 provisional" in items[0].allocation_audit["gross_weight_source"]
    assert sum(i.gross_weight_kg for i in items) == D("100.0000")
    assert all(i.net_weight_kg < i.gross_weight_kg for i in items)


def test_override2_volume_conversion_with_pack_multiplier():
    # spec worked example: 10 CARTONS x 24 x 500 ml shampoo = 120 kg (density 1.00)
    items = [_item(1, "SHAMPOO 24 x 500 ml", qty="10", uom="CTN")]
    allocate_weights_and_cartons(items, {}, D("150"), D("10"), packing_present=False)
    assert items[0].net_weight_kg == D("120.0000")
    assert "description unit conversion" in items[0].allocation_audit["net_weight_source"]
    assert items[0].gross_weight_kg == D("150.0000")         # reconciled to authority


def test_pack_multiplier_not_applied_when_quantity_counts_pieces():
    """The same description billed in PIECES: the quantity already IS the bottle
    count, so multiplying by the pack size would over-declare the line 24x."""
    items = [_item(1, "SHAMPOO 24 x 500 ml", qty="72", uom="EA")]
    allocate_weights_and_cartons(items, {}, D("150"), D("10"), packing_present=False)
    assert items[0].net_weight_kg == D("36.0000")            # 72 x 500 ml, not 72 x 24 x 500 ml
    assert "LOW confidence" in items[0].allocation_audit["net_weight_source"]
    assert any(w.code == "DESCRIPTION_WEIGHT_UNCERTAIN" for w in items[0].warnings)


def test_ambiguous_unit_tokens_do_not_silently_convert():
    # "ST" on a commercial invoice means set/sterile far more often than stone;
    # reading it as 6.35 kg/unit is enough on its own to break a consignment.
    assert net_from_description("GUIDEWIRE PTFE 2 ST", D("5"), "PCS") is None
    # a bare "G" collides with gauge — it still converts, but never at HIGH
    gauge = net_from_description("HYPODERMIC NEEDLE 22 G", D("100"), "PCS")
    assert gauge is not None and gauge.confidence == "LOW" and gauge.warnings
    # an unambiguous spelled-out mass unit is unaffected
    grams = net_from_description("DETERGENT POWDER 500 GRAM", D("40"), "PCS")
    assert grams.net_kg == D("20.000") and grams.confidence == "HIGH"


def test_description_conversions_and_guards():
    assert net_from_description("SHAMPOO 1000ML", D("1")).net_kg == D("1")
    assert net_from_description("PERFUME 100 ml", D("2")).net_kg == D("0.16")      # 0.8 density
    assert net_from_description("PERFUME 100 ml", D("2")).estimated is True
    assert net_from_description("CREAM 500 g", D("3")).net_kg == D("1.5")
    assert net_from_description("RICE 5 kg BAG", D("2")).net_kg == D("10")
    assert net_from_description("COOKING OIL 1 litre", D("1")).net_kg == D("0.92")
    # guards: dosage, bare oz, CBM, plain length never convert
    assert net_from_description("PARACETAMOL 500 mg tablets", D("100")) is None
    assert net_from_description("FACE CREAM 2 oz", D("1")) is None
    assert net_from_description("SOFA SET 1.2 CBM", D("1")) is None
    assert net_from_description("CABLE 100 m", D("1")) is None
    # special formula: GSM with dimensions
    gsm = net_from_description("NONWOVEN FABRIC 1.5 m x 100 m 80 gsm", D("2"))
    assert gsm.net_kg == D("24.000")                          # 2 x (1.5*100*80/1000)


# --------------------------------------------------------------------------- #
# Final validation / reconciliation
# --------------------------------------------------------------------------- #
def test_impossible_carton_total_raises_spec_error():
    items = [_item(i, f"IT{i}") for i in range(1, 6)]        # 5 items, min 0.05 CTN
    msgs = allocate_weights_and_cartons(items, {}, D("10"), D("0.03"), packing_present=False)
    blocking = [m for m in msgs if m.severity.value == "BLOCKING"]
    assert blocking and blocking[0].message == CARTON_TOO_SMALL_MSG


def test_impossible_gross_vs_net_raises_spec_error():
    items = [_item(1, "ALPHA", weight="50", scope="LINE_TOTAL"),
             _item(2, "BETA", weight="60", scope="LINE_TOTAL")]          # nets 110 > gross 100
    msgs = allocate_weights_and_cartons(items, {}, D("100"), D("2"))
    blocking = [m for m in msgs if m.severity.value == "BLOCKING"]
    # the spec's wording is carried verbatim, followed by the actual totals
    assert blocking and blocking[0].message.startswith(GROSS_NOT_ABOVE_NET_MSG)
    assert "110" in blocking[0].message and "100" in blocking[0].message
    # Final Condition 2: impossible input assigns NO weights (never a
    # "best-effort" split that reads as a real allocation)
    assert all(i.gross_weight_kg is None for i in items)


def test_alloy_grade_is_not_read_as_a_volume():
    """"Stainless Steel 316 L" is a steel grade, not 316 litres.

    An ambiguous unit token AND an assumed density is two guesses, and the
    pair declared 316 kg of surgical suture per piece.
    """
    assert net_from_description(
        "Stainless Steel 316 L, Steel, 4, 4 x 45 cm, 1/2 Circle Cutting 48 mm",
        D("5"), "PCS") is None
    # the same token with a KNOWN density is still a volume
    assert net_from_description("SHAMPOO 1 L BOTTLE", D("2"), "PCS").net_kg == D("2")
    # an unambiguous token still converts on an assumed density
    assert net_from_description("HAIR OIL 250 ml", D("4"), "PCS").net_kg == D("0.92")


def test_impossible_description_net_yields_to_the_ratio():
    """A parser reading free text may never make the allocation infeasible.

    Ranks 2/4 are a guess about a description; every other rank is a human or
    a document.  When the guess cannot fit the authority it is rejected and
    the item falls to the ratio — otherwise ONE misread line blanks the gross,
    net and supplementary columns of EVERY item in the shipment.
    """
    items = [_item(1, "DRUM 200 litre", qty="5", uom="PCS"),      # 1000 kg of a 60 kg job
             _item(2, "PLAIN WIDGET", qty="5", uom="PCS"),
             _item(3, "PLAIN WIDGET", qty="5", uom="PCS")]
    msgs = allocate_weights_and_cartons(items, {}, D("60"), D("3"), packing_present=False)
    assert not [m for m in msgs if m.severity.value == "BLOCKING"]
    assert all(i.gross_weight_kg is not None for i in items)      # nothing blanked
    assert sum(i.gross_weight_kg for i in items) == D("60.000")   # still exact
    assert all(i.net_weight_kg < i.gross_weight_kg for i in items)
    rejected = [m for m in msgs if m.code == "DESCRIPTION_NET_REJECTED"]
    assert rejected and "SN 1" in rejected[0].message
    assert "0.7 x gross" in items[0].allocation_audit["net_weight_source"]
    # the innocent rows keep their own source, not the culprit's
    assert "0.7 x gross" in items[1].allocation_audit["net_weight_source"]


def _rejected_trio():
    """Quantities 5/1/4 against invoice values 10/80/10 — deliberately
    divergent, so a quantity share and a value share cannot be confused."""
    return [_item(1, "DRUM 200 litre", qty="5", total="10", uom="PCS"),   # 1000 kg of a 60 kg job
            _item(2, "PLAIN WIDGET", qty="1", total="80", uom="PCS"),
            _item(3, "PLAIN WIDGET", qty="4", total="10", uom="PCS")]


def test_rejected_item_takes_its_exact_quantity_share_of_the_authority():
    """User rule 2026-07-22: a rejected description weight is re-allocated on
    QUANTITY share of the authorised gross — even when the rest of the shipment
    is being split by invoice value, which is a different unit entirely.

    The rescale must make this EXACT, not approximate: SN 1 is 5 of 10 pieces,
    so it takes 5/10 of 60 kg. The others keep invoice-value share for the
    remainder — their own evidence is not disturbed by the rescue.
    """
    items = _rejected_trio()
    # a packing list exists but names nothing usable -> the others sit on value
    packing = match_packing(items, [_packing([{"description_raw": "UNRELATED ROW"}])])
    msgs = allocate_weights_and_cartons(items, packing, D("60"), D("3"), packing_present=True)
    assert any(m.code == "WEIGHT_BASIS_VALUE" for m in msgs)          # others on value
    assert items[0].gross_weight_kg == D("30.000")                    # 5/10 x 60, exactly
    assert "quantity share" in items[0].allocation_audit["gross_weight_source"]
    # remaining 30 kg split 80:10 by value, NOT 1:4 by quantity
    assert items[1].gross_weight_kg > items[2].gross_weight_kg
    assert "invoice value share" in items[1].allocation_audit["gross_weight_source"]
    assert sum(i.gross_weight_kg for i in items) == D("60.000")


def test_packing_list_weight_outranks_the_quantity_share_rescue():
    """A document beats a fallback. When the packing list states a weight for
    the very item whose description was rejected, that weight is the basis —
    the quantity share is for items with no evidence left at all."""
    items = _rejected_trio()
    packing = match_packing(items, [_packing([
        {"description_raw": "DRUM 200 litre", "gross_weight": {"value_raw": "6"}},
        {"description_raw": "PLAIN WIDGET", "gross_weight": {"value_raw": "27"}},
    ])])
    allocate_weights_and_cartons(items, packing, D("60"), D("3"), packing_present=True)
    assert items[0].allocation_audit["gross_weight_source"] == "packing-list gross weight"
    assert items[0].gross_weight_kg != D("30.000")                    # NOT the quantity share
    assert sum(i.gross_weight_kg for i in items) == D("60.000")


def test_stated_values_still_block_when_impossible():
    """The rejection covers PARSER guesses only.  An invoice-printed weight is
    a stated value: it must block so the reviewer corrects it, never be
    silently swapped for a ratio."""
    items = [_item(1, "ALPHA", weight="50", scope="LINE_TOTAL"),
             _item(2, "BETA", weight="60", scope="LINE_TOTAL")]
    msgs = allocate_weights_and_cartons(items, {}, D("100"), D("2"))
    assert [m for m in msgs if m.code == "GROSS_ALLOCATION_IMPOSSIBLE"]
    assert not [m for m in msgs if m.code == "DESCRIPTION_NET_REJECTED"]


def test_cartons_fall_back_to_quantity_when_no_gross_was_allocated():
    """Cartons are not independent of the gross: with no packing carton
    evidence their basis IS the item gross.  When the gross allocation fails
    every basis is zero, which collapsed the apportionment to an EQUAL split
    while the audit still claimed "proportional by gross weight" -- a
    plausible-looking column with nothing behind it, printed beside blank
    Gross and Net.  Quantities 1/9/90 must not produce equal cartons.
    """
    items = [_item(1, "ALPHA", qty="1", weight="50", scope="LINE_TOTAL"),
             _item(2, "BETA", qty="9", weight="60", scope="LINE_TOTAL"),
             _item(3, "GAMMA", qty="90", weight="70", scope="LINE_TOTAL")]
    msgs = allocate_weights_and_cartons(items, {}, D("100"), D("12"))
    assert any(m.code == "GROSS_ALLOCATION_IMPOSSIBLE" for m in msgs)
    assert all(i.gross_weight_kg is None for i in items)           # weights still withheld
    assert [i.package_count for i in items] == [D("0.12"), D("1.08"), D("10.80")]
    assert sum(i.package_count for i in items) == D("12.00")       # still exact
    assert all("quantity share" in i.allocation_audit["carton_source"] for i in items)


def test_partial_gross_failure_does_not_dump_a_line_on_the_carton_floor():
    """Pins overrunning the authority leave only the PINNED item with a gross.
    Basis-ing cartons on that gave the pinned row 9.99 of 10 cartons and
    pushed a 9-piece line onto the 0.01 minimum."""
    items = [_item(1, "PINNED", qty="1"), _item(2, "OTHER", qty="9")]
    items[0].manual_gross_weight_kg = D("150")                     # > the 100 kg authority
    msgs = allocate_weights_and_cartons(items, {}, D("100"), D("10"), packing_present=False)
    assert any(m.code == "REVIEWED_GROSS_EXCEEDS_AUTHORITY" for m in msgs)
    assert [i.package_count for i in items] == [D("1.00"), D("9.00")]
    assert "quantity share" in items[1].allocation_audit["carton_source"]
    assert items[0].allocation_audit["carton_source"] == "proportional by gross weight"


def test_packing_stated_cartons_are_untouched_by_the_gross_fallback():
    """Packing-list cartons really ARE independent of the gross -- that is the
    one case the old blanket comment was right about."""
    items = [_item(1, "ALPHA", qty="1"), _item(2, "BETA", qty="9")]
    packing = match_packing(items, [_packing([
        {"description_raw": "ALPHA", "carton_count": {"value_raw": "3"}},
        {"description_raw": "BETA", "carton_count": {"value_raw": "7"}}])])
    allocate_weights_and_cartons(items, packing, D("100"), D("10"), packing_present=True)
    assert [i.package_count for i in items] == [D("3.00"), D("7.00")]
    assert [i.gross_weight_kg for i in items] == [D("30.000"), D("70.000")]   # gross follows CTN
    assert all(i.allocation_audit["carton_source"] == "packing-list carton" for i in items)


def test_reconciliation_never_reorders_items():
    items = [_item(1, "Z-LAST", qty="1"), _item(2, "A-FIRST", qty="9")]
    allocate_weights_and_cartons(items, {}, D("100"), D("4"), packing_present=False)
    assert [i.xml_item_sequence for i in items] == [1, 2]
    assert [i.description_raw for i in items] == ["Z-LAST", "A-FIRST"]   # never sorted


# --------------------------------------------------------------------------- #
# packing extraction time budget -> fallback + review warning + view flag
# --------------------------------------------------------------------------- #
_INV_RAW = {
    "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
    "page_numbers": [1],
    "header": {"invoice_number_raw": "INV-9", "currency_raw": "USD"},
    "rows": [
        {"source_page_no": 1, "source_row_index": 1, "description_raw": "ALPHA",
         "quantity_raw": "3", "uom_raw": "PCS", "unit_price_raw": "10.00", "line_total_raw": "30.00"},
        {"source_page_no": 1, "source_row_index": 2, "description_raw": "BETA",
         "quantity_raw": "1", "uom_raw": "PCS", "unit_price_raw": "70.00", "line_total_raw": "70.00"},
    ],
}
_PACK_RAW = {
    "role_validation": {"expected_role": "PACKING_LIST", "matches_expected_role": True},
    "rows": [{"source_page_no": 1, "source_row_index": 1, "description_raw": "ALPHA",
              "quantity_raw": "3", "gross_weight": {"value_raw": "90"}},
             {"source_page_no": 1, "source_row_index": 2, "description_raw": "BETA",
              "quantity_raw": "1", "gross_weight": {"value_raw": "10"}}],
    "total_gross_weight": {"value_raw": "100", "unit_raw": "KG"},
    "total_packages": {"value_raw": "4"},
}


def _docs(over_budget: bool):
    pack_warnings = (["PACKING_EXTRACTION_OVER_BUDGET: extraction + reasoning took 431s "
                      "(budget 240s) — fallback will be used."] if over_budget else [])
    return [
        SimpleNamespace(declared_role="INVOICE", upload_index_within_role=0,
                        original_file_name="inv.pdf", raw_extraction=_INV_RAW, warnings=[]),
        SimpleNamespace(declared_role="PACKING_LIST", upload_index_within_role=0,
                        original_file_name="pl.pdf", raw_extraction=_PACK_RAW,
                        warnings=pack_warnings),
    ]


def test_over_budget_packing_falls_back_to_quantity_share_with_warning():
    from app.pipeline import resolve_context, to_critical_review

    ctx = resolve_context(_docs(over_budget=True))
    assert ctx.packing_present is False and ctx.packing_evidence == {}
    review = to_critical_review(ctx, _docs(over_budget=True))
    assert any(w.code == "PACKING_TIMEOUT_FALLBACK" for w in review.warnings)
    assert review.packing_view["validation"]["extraction_over_budget"] is True
    assert "quantity_proportional_fallback" in \
        review.packing_view["allocation_rules_applied"]["item_weight_method"]

    msgs = allocate_weights_and_cartons(
        ctx.items, ctx.packing_evidence, D("100"), D("4"), packing_present=ctx.packing_present)
    # quantity share 3:1 — NOT the packing 90/10 split
    assert [i.gross_weight_kg for i in ctx.items] == [D("75.0000"), D("25.0000")]
    assert any(m.code == "WEIGHT_BASIS_QUANTITY" for m in msgs)


_HEAVY_INV_RAW = {
    **_INV_RAW,
    "rows": [{**r, "item_weight_raw": "80", "item_weight_unit_raw": "KG",
              "item_weight_scope": "LINE_TOTAL"} for r in _INV_RAW["rows"]],
}


def test_preview_states_why_the_weight_columns_are_blank():
    """A blank Detailed Review cell must always carry its reason.

    The preview used to forward three pin-related codes only, so an infeasible
    allocation blanked Gross / Net / Sup_U / Sup_qty on every row while the
    blocking message that explained it was filtered out — the reviewer saw
    four empty columns and five unrelated warnings.
    """
    from app.pipeline import resolve_context, to_critical_review

    docs = [SimpleNamespace(declared_role="INVOICE", upload_index_within_role=0,
                            original_file_name="inv.pdf", raw_extraction=_HEAVY_INV_RAW,
                            warnings=[]),
            SimpleNamespace(declared_role="PACKING_LIST", upload_index_within_role=0,
                            original_file_name="pl.pdf", raw_extraction=_PACK_RAW, warnings=[])]
    review = to_critical_review(resolve_context(docs), docs)

    assert all(not r.gross and not r.net for r in review.item_details)   # the symptom
    codes = {w.code for w in review.warnings}
    assert "GROSS_ALLOCATION_IMPOSSIBLE" in codes                        # ...and its cause
    assert "NET_TO_GROSS_RATIO_IMPLAUSIBLE" in codes                     # ...named
    blocking = [w for w in review.warnings if w.code == "GROSS_ALLOCATION_IMPOSSIBLE"]
    assert blocking[0].severity.value == "BLOCKING"                      # shown in red


def test_preview_groups_repeated_item_messages_by_code():
    """One line per distinct problem naming the affected SNs — not one line
    per item, which would bury the review screen on a 200-row invoice."""
    from app.pipeline import _grouped_item_messages
    from app.domain.errors import ValidationMessage

    items = [SimpleNamespace(xml_item_sequence=n, warnings=[
        ValidationMessage.warning("OLD", "pre-existing, not re-reported"),
        ValidationMessage.blocking("SUPPLEMENTARY_QTY_INVALID", f"Item {n}: net required",
                                   scope="ITEM", item_sequence=n)]) for n in (1, 2, 3)]
    out = _grouped_item_messages([1, 1, 1], items)
    assert len(out) == 1 and out[0].code == "SUPPLEMENTARY_QTY_INVALID"
    assert "SN 1, 2, 3" in out[0].message and out[0].item_sequence is None
    # a lone item keeps its anchor so the row is clickable
    solo = _grouped_item_messages([1], items[:1])
    assert solo[0].item_sequence == 1


def test_within_budget_packing_is_trusted_and_view_matches_invoice_lines():
    from app.pipeline import resolve_context, to_critical_review

    ctx = resolve_context(_docs(over_budget=False))
    assert ctx.packing_present is True
    allocate_weights_and_cartons(ctx.items, ctx.packing_evidence, D("100"), D("4"),
                                 packing_present=True)
    assert [i.gross_weight_kg for i in ctx.items] == [D("90.0000"), D("10.0000")]
    review = to_critical_review(ctx, _docs(over_budget=False))
    assert not any(w.code == "PACKING_TIMEOUT_FALLBACK" for w in review.warnings)
    view = review.packing_view
    assert view["validation"]["extraction_over_budget"] is False
    assert [it["matched_invoice_line_no"] for it in view["items"]] == [1, 2]
    assert view["allocation_rules_applied"]["preserve_invoice_order"] is True
