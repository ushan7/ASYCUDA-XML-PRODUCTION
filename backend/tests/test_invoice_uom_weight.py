"""A line invoiced BY WEIGHT states its own net weight (user rule 2026-08-04).

``RESIN 500 KG`` sells five hundred kilograms of goods: the quantity column IS
that line's net, at LINE_TOTAL scope.  Before this it took a ratio net
(0.7 x an allocated gross) while the invoice stated the figure outright — and
on a KGM tariff code that made the supplementary quantity a derived number
where a stated one existed.

Ladder position (spec section 5): below the printed weight COLUMN, above the
description parser.  Both are invoice sources, so both outrank the packing list.
"""
from decimal import Decimal

from app.rules.models import WorkItem
from app.rules.packing_match import PackingEvidence
from app.rules.weight_carton import allocate_weights_and_cartons, net_from_uom_quantity

D = Decimal


def _item(seq, qty, uom, total="100", desc=None):
    return WorkItem(
        xml_item_sequence=seq, source_invoice_number="INV-1", source_invoice_date="",
        source_invoice_item_index=seq, source_invoice_item_no=None,
        description_raw=desc or f"GOODS {seq}", quantity=D(qty), invoice_uom_raw=uom,
        unit_price=D("1"), line_total=D(total), currency="USD")


def _sources(items):
    return [it.allocation_audit["net_weight_source"] for it in items]


def _codes(items):
    return [w.code for it in items for w in it.warnings]


# --------------------------------------------------------------------------- #
# The reader
# --------------------------------------------------------------------------- #
def test_every_recognized_mass_unit_converts_through_the_ingest_boundary():
    for uom, qty, expect in (("KG", "10", "10.000"), ("KGS", "10", "10.000"),
                             ("kg", "10", "10.000"), ("KGM", "10", "10.000"),
                             ("KILOS", "10", "10.000"), ("G", "2500", "2.500"),
                             ("LBS", "10", "4.536"), ("MT", "2", "2000.000"),
                             ("TONNE", "1", "1000.000")):
        net, src, _ = net_from_uom_quantity(_item(1, qty, uom))
        assert net == D(expect), f"{qty} {uom} -> {net}, expected {expect}"
        assert uom.upper() in src


def test_a_blank_or_non_mass_unit_is_never_read_as_a_weight():
    """`to_kg` treats a blank unit as already-kilograms — right for a weight
    column, catastrophic for a QUANTITY column, where blank means pieces."""
    for uom in ("", "   ", "PCS", "NOS", "CTN", "SET", "DZN", "MTR", "M", "EA", "PAIR"):
        net, src, amb = net_from_uom_quantity(_item(1, "10", uom))
        assert net is None and src == "" and amb is False, f"{uom!r} became a weight"


def test_zero_or_negative_quantity_yields_nothing():
    assert net_from_uom_quantity(_item(1, "0", "KG")) == (None, "", False)


def test_a_uom_carrying_a_pack_size_is_disqualified_not_stripped():
    """`normalize_weight_unit` strips every non-letter, so `25 KG` reduces to a
    clean mass unit and the multiplier vanishes: 200 bags of 25 kg would be
    declared as 200 kg. The pack question ("does the quantity count packs or
    kilograms?") cannot be answered from a unit cell, so the source is
    disqualified — the same rule the spec applies to any unit it cannot trust."""
    for uom in ("25 KG", "500 GM", "1000 KGS", "12 LB", "50KG", "2 X 25 KG"):
        net, src, amb = net_from_uom_quantity(_item(1, "200", uom))
        assert net is None and src == "" and amb is False, f"{uom!r} was read as a bare unit"
    # the bare units themselves are untouched
    assert net_from_uom_quantity(_item(1, "200", "KG"))[0] == D("200.000")
    assert net_from_uom_quantity(_item(1, "200", "LB"))[0] == D("90.718")


def test_a_mass_below_the_declaration_precision_declines_instead_of_declaring_zero():
    """`250 MG` is 0.00025 kg — above zero, but 0.000 at the 3dp the
    declaration carries. Returned as 0.000 it is not None, so the ladder would
    take it as a STATED net outranking every lower rung and declare the item at
    zero net weight."""
    assert net_from_uom_quantity(_item(1, "250", "MG"))[0] is None
    assert net_from_uom_quantity(_item(1, "600", "MG"))[0] == D("0.001")   # still real

    items = [_item(1, "250", "MG"), _item(2, "10", "PCS")]
    allocate_weights_and_cartons(items, {}, D("20"), D("2"), packing_present=False)
    assert all(it.net_weight_kg > 0 for it in items)
    assert all(s.endswith("x gross") for s in _sources(items))             # fell through


def test_the_ambiguous_tokens_convert_but_are_flagged():
    for uom, ambiguous in (("MT", True), ("T", True), ("G", True),
                           ("KG", False), ("KGS", False), ("LB", False), ("OZ", False)):
        _, _, amb = net_from_uom_quantity(_item(1, "10", uom))
        assert amb is ambiguous, f"{uom} ambiguity flag wrong"


# --------------------------------------------------------------------------- #
# In the ladder
# --------------------------------------------------------------------------- #
def test_weighed_lines_keep_their_nets_and_gross_scales_to_the_authority():
    items = [_item(1, "10", "KG"), _item(2, "20", "KGS"), _item(3, "30", "kg")]
    msgs = allocate_weights_and_cartons(items, {}, D("100"), D("10"), packing_present=False)

    assert [it.net_weight_kg for it in items] == [D("10.000"), D("20.000"), D("30.000")]
    assert sum(it.gross_weight_kg for it in items) == D("100.000")
    # the 1.2 CANCELS when every line has a fixed net: gross_i = net_i x A/N
    assert [it.gross_weight_kg for it in items] == [D("16.667"), D("33.333"), D("50.000")]
    assert all(s.startswith("invoice quantity in") for s in _sources(items))
    assert not [m for m in msgs if m.severity.value == "BLOCKING"]


def test_gross_scales_down_when_the_1_2_factor_would_overrun():
    items = [_item(1, "10", "KG"), _item(2, "20", "KG"), _item(3, "30", "KG")]
    allocate_weights_and_cartons(items, {}, D("65"), D("10"), packing_present=False)

    assert [it.net_weight_kg for it in items] == [D("10.000"), D("20.000"), D("30.000")]
    assert sum(it.gross_weight_kg for it in items) == D("65.000")
    for it in items:                                   # net < gross still holds
        assert it.net_weight_kg < it.gross_weight_kg


def test_mixed_shipment_leaves_unweighed_lines_on_the_ratio():
    items = [_item(1, "2", "KG"), _item(2, "500", "PCS")]
    allocate_weights_and_cartons(items, {}, D("100"), D("10"), packing_present=False)

    assert items[0].net_weight_kg == D("2.000")
    assert _sources(items)[0] == "invoice quantity in KG"
    assert _sources(items)[1].endswith("x gross")       # ratio mode
    assert sum(it.gross_weight_kg for it in items) == D("100.000")


def test_printed_weight_column_outranks_the_mass_unit_quantity():
    items = [_item(1, "500", "KG"), _item(2, "10", "KG")]
    items[0].item_weight_kg = D("4")                   # an explicit NET column
    items[0].item_weight_scope = "LINE_TOTAL"
    allocate_weights_and_cartons(items, {}, D("1000"), D("10"), packing_present=False)

    assert items[0].net_weight_kg == D("4.000")
    assert _sources(items)[0] == "invoice weight override"
    # ...and the 125x disagreement is reported rather than resolved in silence
    disagree = [w for w in items[0].warnings if w.code == "NET_WEIGHT_SOURCES_DISAGREE"]
    assert len(disagree) == 1
    assert "500.000 kg" in disagree[0].message and "4.000 kg" in disagree[0].message


def test_mass_unit_quantity_outranks_the_packing_list_net():
    items = [_item(1, "10", "KG"), _item(2, "10", "KG")]
    packing = {1: PackingEvidence(net_weight=D("99"), gross_weight=D("120"), matched=True)}
    allocate_weights_and_cartons(items, packing, D("100"), D("10"), packing_present=True)

    assert items[0].net_weight_kg == D("10.000")       # the invoice wins
    assert _sources(items)[0] == "invoice quantity in KG"


def test_ambiguous_unit_warns_on_the_item():
    items = [_item(1, "2", "MT"), _item(2, "5", "KG")]
    allocate_weights_and_cartons(items, {}, D("5000"), D("10"), packing_present=False)

    assert items[0].net_weight_kg == D("2000.000")     # x1000, not x1
    amb = [w for w in items[0].warnings if w.code == "INVOICE_UOM_WEIGHT_AMBIGUOUS"]
    assert len(amb) == 1 and "MT" in amb[0].message
    assert "INVOICE_UOM_WEIGHT_AMBIGUOUS" not in _codes([items[1]])


def test_ambiguity_is_silent_when_a_higher_rung_takes_the_row():
    """Warning about a reading that was never used sends the reviewer to check
    a number the declaration does not contain."""
    items = [_item(1, "2", "MT"), _item(2, "5", "KG")]
    items[0].item_weight_kg = D("1900")                # printed column wins
    items[0].item_weight_scope = "LINE_TOTAL"
    allocate_weights_and_cartons(items, {}, D("5000"), D("10"), packing_present=False)

    assert items[0].net_weight_kg == D("1900.000")
    assert "INVOICE_UOM_WEIGHT_AMBIGUOUS" not in _codes([items[0]])

    # a reviewer pin silences it too
    pinned = [_item(1, "2", "MT"), _item(2, "5", "KG")]
    pinned[0].manual_net_weight_kg = D("1800")
    allocate_weights_and_cartons(pinned, {}, D("5000"), D("10"), packing_present=False)
    assert "INVOICE_UOM_WEIGHT_AMBIGUOUS" not in _codes([pinned[0]])


def test_a_stated_net_that_overruns_blocks_and_names_a_workable_authority():
    """A mass-unit quantity is a DOCUMENT statement, so it blocks rather than
    being dropped the way a description-parsed net is — and the message says
    what the authority would have to be, for the one-click fix."""
    items = [_item(1, "60", "KG"), _item(2, "40", "KG")]
    msgs = allocate_weights_and_cartons(items, {}, D("59"), D("10"), packing_present=False)

    blocking = [m for m in msgs if m.severity.value == "BLOCKING"]
    hit = next(m for m in blocking if m.code == "GROSS_ALLOCATION_IMPOSSIBLE")
    assert hit.remediation == "Set the shipment gross weight to at least 100.002 kg."
    assert all(it.gross_weight_kg is None for it in items)     # nothing assigned

    # and that authority does in fact work
    ok = [_item(1, "60", "KG"), _item(2, "40", "KG")]
    msgs2 = allocate_weights_and_cartons(ok, {}, D("100.002"), D("10"), packing_present=False)
    assert not [m for m in msgs2 if m.severity.value == "BLOCKING"]
    assert sum(it.gross_weight_kg for it in ok) == D("100.002")


def test_supplementary_quantity_uses_the_stated_weight_on_a_kgm_code():
    """The payoff: on a KGM tariff code the supplementary quantity is derived
    from the net, so a weighed line now declares the invoiced figure instead of
    one derived from a ratio."""
    from app.rules.supplementary_unit import resolve_supplementary_all

    items = [_item(1, "40", "KG"), _item(2, "60", "KG")]
    for it in items:
        it.hs_tariff_unit = "KGM"
    allocate_weights_and_cartons(items, {}, D("200"), D("10"), packing_present=False)
    resolve_supplementary_all(items)

    assert [it.supplementary_quantity for it in items] == [D("40.0000"), D("60.0000")]
    assert all(it.supplementary_unit_code == "KGM" for it in items)


def test_description_conversion_still_loses_to_the_quantity_unit():
    """Rank 3 beats rank 4: the unit of sale is a document field, the
    description is a parser reading free text."""
    items = [_item(1, "3", "KG", desc="WIDGET 500 ML BOTTLE"), _item(2, "3", "KG")]
    allocate_weights_and_cartons(items, {}, D("100"), D("10"), packing_present=False)
    assert items[0].net_weight_kg == D("3.000")
    assert _sources(items)[0] == "invoice quantity in KG"
