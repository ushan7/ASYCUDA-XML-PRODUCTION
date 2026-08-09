"""Packing evidence must reach invoice items BY IDENTITY, IN INVOICE ORDER.

Two spec rules meet here and both are silent when broken.

``docs/allocation-spec.md`` §2 — *the XML goods items follow the invoice item
order exactly; never sort by packing-list order*. The implementation contract
that enforces it is narrow and easy to lose: ``match_packing`` returns a dict
KEYED BY ``xml_item_sequence``, never a list, and ``weight_carton`` computes in
place. A list would silently re-key every weight to the packing list's own row
order the moment a supplier typed their rows in a different sequence.

§3 — *packing rows are matched by normalized product identity, never by row
number*. A shuffled packing list must therefore produce byte-identical
allocation to an aligned one. That is the property tested here, because it is
the only way to prove ordering is not being relied on by accident.

§6 — the two absolutes: allocated gross sums EXACTLY to the authorised total,
and cartons sit on the 0.01 lattice and sum exactly. A distribution that is
merely close is a declaration that does not add up.

These are property tests, not fixture tests: they compare a shuffled document
against its aligned twin, so they fail on any reintroduction of positional
assignment regardless of which layer causes it.
"""
from decimal import Decimal

import pytest

from app.extraction.common_models import (
    PackingListChunkRaw, PackingRowRaw, RawNumber, RoleValidation)
from app.domain.enums import DeclaredRole
from app.rules.models import WorkItem
from app.rules.packing_match import match_packing
from app.rules.weight_carton import allocate_weights_and_cartons

# Deliberately similar names so identity matching has to do real work, but
# distinct enough that the spec's "never merge differing size/variant" holds.
GOODS = [
    ("STAINLESS STEEL VACUUM FLASK 500ML", Decimal("100"), Decimal("250.00")),
    ("STAINLESS STEEL VACUUM FLASK 750ML", Decimal("80"), Decimal("240.00")),
    ("STAINLESS STEEL LUNCH BOX 1.2L", Decimal("60"), Decimal("300.00")),
    ("BAMBOO CUTTING BOARD LARGE", Decimal("40"), Decimal("120.00")),
]
# weight/carton evidence, keyed by the SAME description
EVIDENCE = {
    "STAINLESS STEEL VACUUM FLASK 500ML": ("720.00", "800.00", "50"),
    "STAINLESS STEEL VACUUM FLASK 750ML": ("675.00", "742.50", "45"),
    "STAINLESS STEEL LUNCH BOX 1.2L": ("750.00", "857.50", "25"),
    "BAMBOO CUTTING BOARD LARGE": ("300.00", "340.00", "20"),
}


def _items():
    return [
        WorkItem(xml_item_sequence=i + 1, source_invoice_number="INV-1",
                 source_invoice_date="2026-01-01", source_invoice_item_index=i,
                 source_invoice_item_no=str(i + 1), description_raw=desc,
                 quantity=qty, invoice_uom_raw="PCS",
                 unit_price=(total / qty), line_total=total, currency="USD")
        for i, (desc, qty, total) in enumerate(GOODS)]


def _packing(order):
    rows = []
    for n, desc in enumerate(order, start=1):
        net, gross, ctn = EVIDENCE[desc]
        rows.append(PackingRowRaw(
            source_page_no=1, source_row_index=n, description_raw=desc,
            gross_weight=RawNumber(value_raw=gross, unit_raw="KGS"),
            net_weight=RawNumber(value_raw=net, unit_raw="KGS"),
            carton_count=RawNumber(value_raw=ctn, unit_raw="CTN")))
    return PackingListChunkRaw(
        role_validation=RoleValidation(expected_role=DeclaredRole.PACKING_LIST,
                                       matches_expected_role=True),
        rows=rows)


ALIGNED = [d for d, _, _ in GOODS]
SHUFFLED = [GOODS[3][0], GOODS[1][0], GOODS[3 - 3][0], GOODS[2][0]]   # 4,2,1,3


def test_match_is_keyed_by_item_sequence_not_a_list():
    """The contract §2 names: a dict keyed by xml_item_sequence.  A list here
    would re-key every weight to packing-row order without anything failing."""
    got = match_packing(_items(), [_packing(ALIGNED)])
    assert isinstance(got, dict)
    assert set(got) <= {1, 2, 3, 4}


@pytest.mark.parametrize("order", [ALIGNED, SHUFFLED])
def test_each_item_receives_its_own_packing_weight(order):
    """Identity, never row number: item 1 gets flask-500ML's weights whether the
    packing list lists it first or third."""
    got = match_packing(_items(), [_packing(order)])
    for i, (desc, _, _) in enumerate(GOODS, start=1):
        net, gross, ctn = EVIDENCE[desc]
        ev = got.get(i)
        assert ev is not None and ev.matched, f"item {i} ({desc}) went unmatched"
        assert ev.gross_weight == Decimal(gross)
        assert ev.net_weight == Decimal(net)
        assert ev.carton_count == Decimal(ctn)


def test_shuffling_the_packing_list_changes_nothing():
    """The property that proves ordering is not relied on anywhere in between."""
    a = match_packing(_items(), [_packing(ALIGNED)])
    b = match_packing(_items(), [_packing(SHUFFLED)])
    assert {k: (v.gross_weight, v.net_weight, v.carton_count) for k, v in a.items()} == \
           {k: (v.gross_weight, v.net_weight, v.carton_count) for k, v in b.items()}


@pytest.mark.parametrize("order", [ALIGNED, SHUFFLED])
def test_allocation_preserves_invoice_order_and_sums_exactly(order):
    """§6's two absolutes, checked on the allocated result rather than asserted
    about the code: items stay in invoice order, gross sums EXACTLY to the
    authorised total, and every carton sits on the 0.01 lattice."""
    items = _items()
    auth_gross = Decimal("2740.00")          # 800.00+742.50+857.50+340.00
    auth_ctn = Decimal("140")                # 50+45+25+20
    allocate_weights_and_cartons(
        items, match_packing(items, [_packing(order)]),
        authorized_total_gross=auth_gross, authorized_total_packages=auth_ctn)

    assert [it.xml_item_sequence for it in items] == [1, 2, 3, 4]
    assert [it.description_raw for it in items] == [d for d, _, _ in GOODS]

    assert sum((it.gross_weight_kg or Decimal(0)) for it in items) == auth_gross
    assert sum((it.package_count or Decimal(0)) for it in items) == auth_ctn
    for it in items:
        ctn = it.package_count or Decimal(0)
        assert ctn == ctn.quantize(Decimal("0.01")), "carton off the 0.01 lattice"
        assert (it.net_weight_kg or Decimal(0)) <= (it.gross_weight_kg or Decimal(0))


def test_printed_packing_weights_are_used_when_nothing_outranks_them():
    """With no description volume/mass to convert, the printed packing figures
    ARE the allocation — apportioning them away would replace stated evidence
    with arithmetic."""
    plain = ["STEEL BRACKET HEAVY", "STEEL BRACKET LIGHT",
             "TIMBER BATTEN ROUGH", "COPPER FITTING ELBOW"]
    items = _items()
    for it, desc in zip(items, plain):        # strip the "500ML"/"1.2L" tokens
        it.description_raw = desc
    rows = _packing(ALIGNED).rows
    for r, desc in zip(rows, plain):
        r.description_raw = desc
    pl = _packing(ALIGNED)
    pl.rows = rows
    allocate_weights_and_cartons(
        items, match_packing(items, [pl]),
        authorized_total_gross=Decimal("2740.00"),
        authorized_total_packages=Decimal("140"))
    assert [it.gross_weight_kg for it in items] == [
        Decimal("800.00"), Decimal("742.50"), Decimal("857.50"), Decimal("340.00")]
    assert [it.package_count for it in items] == [
        Decimal("50"), Decimal("45"), Decimal("25"), Decimal("20")]


def test_the_packing_list_net_outranks_a_description_conversion():
    """§5 ranks 4/5, swapped 2026-08-04: a weight the packing list PRINTS beats a
    weight inferred from a product NAME.

    "VACUUM FLASK 500ML" x 100 used to read as 500 ml of water — 50 kg, at LOW
    confidence — and replace the printed 720.00 kg.  Worse, the exact-sum
    absolute then pushed the freed gross onto the other lines, so items nobody
    had misread also lost their printed figures.  Every line here states its
    weight on the packing list, so every line must show it."""
    items = _items()
    allocate_weights_and_cartons(
        items, match_packing(items, [_packing(SHUFFLED)]),
        authorized_total_gross=Decimal("2740.00"),
        authorized_total_packages=Decimal("140"))
    assert [it.net_weight_kg for it in items] == [
        Decimal("720.00"), Decimal("675.00"), Decimal("750.00"), Decimal("300.00")]
    assert [it.gross_weight_kg for it in items] == [
        Decimal("800.00"), Decimal("742.50"), Decimal("857.50"), Decimal("340.00")]
    for it in items:
        assert it.allocation_audit["net_weight_source"] == "packing-list net weight"
    assert [it.package_count for it in items] == [
        Decimal("50"), Decimal("45"), Decimal("25"), Decimal("20")]
    assert sum(it.gross_weight_kg for it in items) == Decimal("2740.00")


CTN_GOODS = {"PAPER CARTOON BOX": 3, "PRINTER PARTS HOT ROLLER": 6,
             "SCHOOL BAG": 9, "KEYBOARD": 100, "SPEAKER": 25}      # sums to 143


def _ctn_case(auth):
    from app.rules.packing_match import PackingEvidence

    items, ev = [], {}
    for i, (d, ctn) in enumerate(CTN_GOODS.items(), start=1):
        items.append(WorkItem(
            xml_item_sequence=i, source_invoice_number="I", source_invoice_date="d",
            source_invoice_item_index=i - 1, source_invoice_item_no=str(i),
            description_raw=d, quantity=Decimal("10"), invoice_uom_raw="PCS",
            unit_price=Decimal("10"), line_total=Decimal("100"), currency="USD"))
        ev[i] = PackingEvidence(carton_count=Decimal(ctn), matched=True,
                                match_confidence=Decimal(1))
    w = allocate_weights_and_cartons(
        items, ev, authorized_total_gross=Decimal("1000"),
        authorized_total_packages=Decimal(auth))
    return items, w


def test_printed_carton_counts_pass_through_exactly():
    """When the packing list gives a carton count for every item and those
    counts agree with the authority, each item declares the count the DOCUMENT
    prints — no apportionment, no fractions of a physical box."""
    items, w = _ctn_case(143)
    assert [it.package_count for it in items] == [
        Decimal("3.00"), Decimal("6.00"), Decimal("9.00"),
        Decimal("100.00"), Decimal("25.00")]
    assert "PACKING_CTN_TOTAL_MISMATCH" not in [x.code for x in w]
    assert items[0].allocation_audit["carton_source"] == "packing-list carton"


def test_rescaling_printed_carton_counts_is_loud_and_attributed():
    """The live 2026-08-04 failure: a 385-vs-492 shortfall multiplied all
    fifteen printed counts by 1.2779 and declared 2.56 and 6.39 physical
    cartons — silently, with `carton_source` still reading "packing-list
    carton".  One misread row moves EVERY count, so the divergence has to be
    reported and each value has to stop claiming a provenance it no longer has.

    The same haircut forced by a reviewer pin was already loud
    (CTN_PIN_RESCALED_PACKING_EVIDENCE); the authority path must not be quieter."""
    items, w = _ctn_case(160)
    msg = next((x for x in w if x.code == "PACKING_CTN_TOTAL_MISMATCH"), None)
    assert msg is not None, "a printed carton count was rescaled with no message"
    assert "143" in msg.message and "160" in msg.message
    assert msg.remediation and "143" in msg.remediation
    src = items[0].allocation_audit["carton_source"]
    assert src.startswith("packing-list carton")
    assert "scaled x1.1189" in src and "states 3.00" in src
    assert sum(it.package_count for it in items) == Decimal("160.00")


LIVE_PAGE = """
| S.NO | DESCRIPTION | CTN NO | QTY CTN | CTN | TOTAL QTY | UNIT | MARKA |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | PRINTER PARTS HOT ROLLER | 13 | 60 | 1 | 60 | PCS | JIA-8 |
| 2 | PRINTER PARTS HOT ROLLER | 16-20 | 100 | 5 | 500 | PCS |
| 3 | FLAT CONVERSE SHOES | 90-90 | 20 | 30 | 600 | PRS | HKDC-90 |
| 4 | FLAT CONVERSE SHOES | 20 | 10 | 200 | PRS |
| 5 | SMALL LAMINATION MACHINE | 1-25 | 4 | 25 | 100 | PCS | LIANABIN-25 |
| 6 | LADIES BAG | 2 | 10 | 1 | 10 | PCS | JIH-9 |
| 7 | FUR CLOTH | 1-23 | 26 | 23 | 598 | KG | SOM-155 |
| 8 | SPEAKER | 1-25 | 1 | 25 | 25 | PCS | SKAN-25 |
| TOTAL | 150 | 2093 |  |  |
"""


def _live_rows():
    from app.domain.enums import DeclaredRole as _R
    from app.extraction.table_parser import parse_pages
    from app.numbers import detect_numeric_locale
    res = parse_pages(_R.PACKING_LIST, {1: LIVE_PAGE},
                      {1: detect_numeric_locale(LIVE_PAGE)})
    return [r for pp in res.pages.values() if pp.confirmed for r in pp.rows], res


def test_the_carton_count_column_is_read_not_the_carton_number_range():
    """"CTN NO | QTY CTN | CTN" means WHICH cartons, quantity PER carton, and
    HOW MANY.  First-match-wins gave the count column away twice over, so the
    counts came from measuring carton-number ranges instead — 385 against a
    printed 492 on the live document, and 30 keyboards on a row of 3000."""
    rows, _ = _live_rows()
    assert [r.carton_count.value_raw for r in rows] == ["1", "5", "30", "10", "25", "1", "23", "25"]
    assert [r.quantity_raw for r in rows] == ["60", "500", "600", "200", "100", "10", "598", "25"]


def test_a_row_whose_ocr_dropped_a_cell_is_realigned_by_its_own_arithmetic():
    """Row 4 prints no CTN NO cell, so six cells arrive against an eight-column
    header and every column shifts — 200 cartons on a row that packs 10.
    Disowning the page to the LLM is safe but costs the whole document; the gap
    is located instead, and accepted only because qty-per-carton x cartons ==
    quantity holds at exactly one placement."""
    rows, res = _live_rows()
    assert len(rows) == 8, "the ragged row must be recovered, not sent to the LLM"
    assert rows[3].carton_count.value_raw == "10"      # not 200
    assert rows[3].quantity_raw == "200"
    assert any("realigned and proved by" in n for n in res.notes)


def test_the_same_carton_id_far_apart_is_two_carton_sets_not_one():
    """Carton numbering restarts per shipping mark.  SMALL LAMINATION MACHINE
    (LIANABIN-25) and SPEAKER (SKAN-25) both print "1-25" but are rows apart —
    merging them took the group total as 25 instead of 50 and deleted 25 of the
    shipment's cartons."""
    rows, _ = _live_rows()
    assert rows[4].shared_carton_group_raw is None
    assert rows[7].shared_carton_group_raw is None
    assert rows[4].carton_count.value_raw == "25"
    assert rows[7].carton_count.value_raw == "25"


def test_repeated_rows_of_one_item_sum_their_cartons():
    """PRINTER PARTS HOT ROLLER prints twice — 1 carton and 5 — and the invoice
    has one line for it.  Spec section 3: repeated rows of the same normalized
    item are grouped and SUMMED before assignment."""
    from app.extraction.common_models import PackingListChunkRaw as _PL
    rows, _ = _live_rows()
    pl = _PL(role_validation=RoleValidation(expected_role=DeclaredRole.PACKING_LIST,
                                            matches_expected_role=True), rows=rows)
    names = ["PRINTER PARTS HOT ROLLER", "FLAT CONVERSE SHOES",
             "SMALL LAMINATION MACHINE", "SPEAKER"]
    items = [WorkItem(xml_item_sequence=i, source_invoice_number="I",
                      source_invoice_date="d", source_invoice_item_index=i - 1,
                      source_invoice_item_no=str(i), description_raw=d,
                      quantity=Decimal("10"), invoice_uom_raw="PCS",
                      unit_price=Decimal("10"), line_total=Decimal("100"),
                      currency="USD") for i, d in enumerate(names, start=1)]
    ev = match_packing(items, [pl])
    assert ev[1].carton_count == Decimal("6")     # 1 + 5 summed
    assert ev[2].carton_count == Decimal("40")    # 30 + 10 summed
    assert ev[3].carton_count == Decimal("25")
    assert ev[4].carton_count == Decimal("25")
    allocate_weights_and_cartons(
        items, ev, authorized_total_gross=Decimal("1000"),
        authorized_total_packages=Decimal("96"))
    assert [it.package_count for it in items] == [
        Decimal("6.00"), Decimal("40.00"), Decimal("25.00"), Decimal("25.00")]


def _by_weight(sn, desc, qty, uom, total):
    return WorkItem(xml_item_sequence=sn, source_invoice_number="I", source_invoice_date="d",
                    source_invoice_item_index=sn - 1, source_invoice_item_no=str(sn),
                    description_raw=desc, quantity=qty, invoice_uom_raw=uom,
                    unit_price=(total / qty), line_total=total, currency="USD")


def test_gross_never_exceeds_1_2x_a_net_the_invoice_states():
    """User rule 2026-08-04.  ``EPOXY RESIN 500 KGM`` sells 500 kg — that IS the
    line's net — so its gross may not exceed 600 kg.

    `net x 1.2` was only ever a proportional WEIGHTING, and §6's exact-sum rule
    rescales every share against the authority: two heavier unweighed lines
    pulled the basis up and this line took 1636.364 kg gross against a stated
    500 kg net.  The factor is now a CEILING, and the weight it releases goes to
    the lines the invoice says nothing about."""
    items = [_by_weight(1, "EPOXY RESIN", Decimal("500"), "KGM", Decimal("2500")),
             _by_weight(2, "PLASTIC CRATE", Decimal("200"), "PCS", Decimal("4000")),
             _by_weight(3, "STEEL CLAMP", Decimal("300"), "PCS", Decimal("3000"))]
    allocate_weights_and_cartons(
        items, {}, authorized_total_gross=Decimal("3000.00"),
        authorized_total_packages=Decimal("120"), packing_present=False)
    assert items[0].net_weight_kg == Decimal("500.000")
    assert items[0].gross_weight_kg == Decimal("600.000")     # exactly 1.2 x 500
    assert items[0].allocation_audit["net_weight_source"].startswith("invoice quantity in")
    assert sum(it.gross_weight_kg for it in items) == Decimal("3000.000")


def test_the_cap_binds_the_invoice_printed_weight_column_too():
    """Rank 2 is the same kind of statement as rank 3 and takes the same cap."""
    items = [_by_weight(1, "STEEL COIL", Decimal("10"), "PCS", Decimal("5000")),
             _by_weight(2, "PACKING TIMBER", Decimal("50"), "PCS", Decimal("500"))]
    items[0].item_weight_kg = Decimal("400.00")      # invoice prints 400 kg total
    items[0].item_weight_scope = "LINE_TOTAL"
    allocate_weights_and_cartons(
        items, {}, authorized_total_gross=Decimal("2000.00"),
        authorized_total_packages=Decimal("20"), packing_present=False)
    assert items[0].net_weight_kg == Decimal("400.000")
    assert items[0].gross_weight_kg <= Decimal("480.000")     # 1.2 x 400
    assert sum(it.gross_weight_kg for it in items) == Decimal("2000.000")


def test_a_packing_list_net_is_not_capped_its_own_gross_stands():
    """The cap is for INVOICE-stated nets only.  A packing list prints its own
    gross beside its net, and that pair is the document's own statement — 800
    against 720 is a 1.11 ratio the shipper measured, not one we impose."""
    items = _items()
    allocate_weights_and_cartons(
        items, match_packing(items, [_packing(ALIGNED)]),
        authorized_total_gross=Decimal("2740.00"),
        authorized_total_packages=Decimal("140"))
    assert [it.gross_weight_kg for it in items] == [
        Decimal("800.00"), Decimal("742.50"), Decimal("857.50"), Decimal("340.00")]


def test_an_impossible_cap_is_reported_and_released_never_silently_broken():
    """When the authorised gross cannot fit under the caps the two sources
    genuinely disagree.  The declaration must still add up, so the cap is
    released — but loudly, because that disagreement is the reviewer's call."""
    items = [_by_weight(1, "EPOXY RESIN", Decimal("100"), "KGM", Decimal("500")),
             _by_weight(2, "HARDENER", Decimal("50"), "KGM", Decimal("400"))]
    w = allocate_weights_and_cartons(
        items, {}, authorized_total_gross=Decimal("5000.00"),
        authorized_total_packages=Decimal("40"), packing_present=False)
    assert "GROSS_EXCEEDS_INVOICE_WEIGHT_CAP" in [x.code for x in w]
    assert sum(it.gross_weight_kg for it in items) == Decimal("5000.000")


def test_an_invoice_stated_weight_still_overrides_the_packing_list():
    """Ranks 2 and 3 are untouched: when the INVOICE states an item's weight, it
    governs — for the items that have one.  Items without one fall through to
    the packing list, so the two sources coexist on one declaration."""
    items = _items()
    items[0].item_weight_kg = Decimal("6.00")        # invoice prints 6 kg/unit
    items[0].item_weight_scope = "PER_UNIT"          # x 100 units = 600 kg
    allocate_weights_and_cartons(
        items, match_packing(items, [_packing(ALIGNED)]),
        authorized_total_gross=Decimal("2740.00"),
        authorized_total_packages=Decimal("140"))
    assert items[0].net_weight_kg == Decimal("600.00")
    assert items[0].allocation_audit["net_weight_source"] == "invoice weight override"
    # the lines the invoice says nothing about keep the packing list's figures
    assert [it.net_weight_kg for it in items[1:]] == [
        Decimal("675.00"), Decimal("750.00"), Decimal("300.00")]
    for it in items[1:]:
        assert it.allocation_audit["net_weight_source"] == "packing-list net weight"


# --------------------------------------------------------------------------- #
# Scientific weight/carton distribution (2026-08-04)
# --------------------------------------------------------------------------- #
def _mk(spec):
    """(description, uom, quantity, packing gross or None) -> items + evidence."""
    from app.rules.packing_match import PackingEvidence

    items, ev = [], {}
    for i, (d, u, q, g) in enumerate(spec, start=1):
        items.append(WorkItem(
            xml_item_sequence=i, source_invoice_number="I", source_invoice_date="d",
            source_invoice_item_index=i - 1, source_invoice_item_no=str(i),
            description_raw=d, quantity=q, invoice_uom_raw=u,
            unit_price=Decimal("1"), line_total=q, currency="USD"))
        if g is not None:
            ev[i] = PackingEvidence(gross_weight=g, matched=True,
                                    match_confidence=Decimal(1))
    return items, ev


def test_cartons_come_from_units_per_carton_exactly_when_printed():
    """A row printing "20 PRS per carton" against a total of 600 PRS packs
    THIRTY cartons and the document said so — that is exact, where measuring a
    carton-number range or apportioning by weight is only proportional."""
    from app.domain.enums import DeclaredRole as _R
    from app.extraction.table_parser import parse_pages
    from app.numbers import detect_numeric_locale

    page = """
| S/N | DESCRIPTION | QTY/CTN | TOTAL QTY | UNIT | N.W. (KGS) | G.W. (KGS) |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | FLAT CONVERSE SHOES | 20 | 600 | PRS | 540.00 | 600.00 |
| 2 | KEYBOARD | 30 | 3000 | PCS | 900.00 | 990.00 |
| 3 | SPEAKER | 1 | 25 | PCS | 60.00 | 70.00 |
| TOTAL |  |  | 3625 | | 1500.00 | 1660.00 |
"""
    res = parse_pages(_R.PACKING_LIST, {1: page}, {1: detect_numeric_locale(page)})
    rows = [r for pp in res.pages.values() if pp.confirmed for r in pp.rows]
    assert [r.carton_count.value_raw for r in rows] == ["30", "100", "25"]


def test_a_non_integer_units_per_carton_result_is_refused():
    """601 PRS at 20 per carton is not 30.05 cartons — the identity does not
    hold, so nothing is derived and the ordinary fallbacks take over."""
    from app.domain.enums import DeclaredRole as _R
    from app.extraction.table_parser import parse_pages
    from app.numbers import detect_numeric_locale

    page = """
| S/N | DESCRIPTION | QTY/CTN | TOTAL QTY | UNIT | G.W. (KGS) |
| --- | --- | --- | --- | --- | --- |
| 1 | FLAT CONVERSE SHOES | 20 | 601 | PRS | 600.00 |
| 2 | KEYBOARD | 30 | 3000 | PCS | 990.00 |
| TOTAL |  | 3601 | | 1590.00 |
"""
    res = parse_pages(_R.PACKING_LIST, {1: page}, {1: detect_numeric_locale(page)})
    rows = [r for pp in res.pages.values() if pp.confirmed for r in pp.rows]
    assert rows and rows[0].carton_count is None      # 601/20 is not whole
    assert rows[1].carton_count.value_raw == "100"    # 3000/30 is


def test_unmatched_weight_is_estimated_per_unit_of_sale_not_globally():
    """A metre of cloth and a pair of shoes do not weigh alike, so one global
    kilograms-per-dollar is the wrong shape for the whole shipment.  Items sold
    in the SAME unit do weigh alike, and the shipment teaches the rate."""
    items, ev = _mk([("SHOE A", "PRS", Decimal("600"), Decimal("600")),
                     ("SHOE B", "PRS", Decimal("400"), None),
                     ("CLOTH A", "MTR", Decimal("10000"), Decimal("500")),
                     ("CLOTH B", "MTR", Decimal("4000"), None)])
    allocate_weights_and_cartons(items, ev, authorized_total_gross=Decimal("1700"),
                                 authorized_total_packages=Decimal("100"))
    assert items[1].gross_weight_kg == Decimal("400.000")     # 1.00 kg/PRS
    assert items[3].gross_weight_kg == Decimal("200.000")     # 0.05 kg/MTR
    assert "per PRS learned from this shipment" in \
        items[1].allocation_audit["gross_weight_source"]
    assert "per MTR learned from this shipment" in \
        items[3].allocation_audit["gross_weight_source"]
    assert sum(it.gross_weight_kg for it in items) == Decimal("1700")


def test_an_impossible_per_unit_weight_is_reported_not_corrected():
    """A declaration can reconcile exactly and still claim 90 kg for a pair of
    shoes.  The band is the shipment's own median for that unit, so no external
    table is needed — and the figure is REPORTED, never rewritten."""
    items, ev = _mk([("SHOE A", "PRS", Decimal("600"), Decimal("600")),
                     ("SHOE B", "PRS", Decimal("400"), Decimal("400")),
                     ("SHOE C", "PRS", Decimal("200"), Decimal("210")),
                     ("SHOE D", "PRS", Decimal("100"), Decimal("9000"))])
    w = allocate_weights_and_cartons(items, ev, authorized_total_gross=Decimal("10210"),
                                     authorized_total_packages=Decimal("100"))
    flagged = [m for m in w if m.code == "ITEM_UNIT_WEIGHT_IMPLAUSIBLE"]
    assert [m.item_sequence for m in flagged] == [4]
    assert items[3].gross_weight_kg == Decimal("9000.000")   # reported, not corrected
    assert sum(it.gross_weight_kg for it in items) == Decimal("10210")


def test_a_unit_the_shipment_cannot_teach_is_not_judged():
    """One stated row in a unit is not a band — judging against a sample of one
    would report every second item of an ordinary mixed consignment."""
    items, ev = _mk([("SHOE A", "PRS", Decimal("600"), Decimal("600")),
                     ("ANVIL", "PCS", Decimal("1"), Decimal("400"))])
    w = allocate_weights_and_cartons(items, ev, authorized_total_gross=Decimal("1000"),
                                     authorized_total_packages=Decimal("10"))
    assert [m for m in w if m.code == "ITEM_UNIT_WEIGHT_IMPLAUSIBLE"] == []
