"""Regression pins for the 2026-07-30 adversarial audit.

Sixteen confirmed defects, every one a silent wrong number on a customs
declaration: full pages parsed and owned while a goods row was swallowed as a
header, per-carton rates declared as row totals, carton ranges destroyed by
the serial heuristic, shared groups split by an OCR space, purchase orders
read as carton ranges, pack sizes matched as product codes, and seven ways a
description conversion produced a wrong weight at HIGH confidence.

Each test reproduces the audit's demonstrated input.
"""
from decimal import Decimal

import pytest

from app.domain.enums import DeclaredRole
from app.extraction.common_models import PackingListChunkRaw, RoleValidation
from app.extraction.table_parser import _cells, _header_map, parse_pages
from app.rules.description_weight import net_from_description
from app.rules.models import WorkItem
from app.rules.packing_match import _codes, match_packing
from app.rules.weight_carton import allocate_weights_and_cartons

D = Decimal


@pytest.fixture(autouse=True)
def _isolate_layout_store(tmp_path, monkeypatch):
    from app import config as config_mod
    from app.extraction import layout_memory
    s = config_mod.get_settings_uncached()
    s.storage_dir = tmp_path
    monkeypatch.setattr(layout_memory, "get_settings", lambda: s)


def _parse(pages, locales=None):
    return parse_pages(DeclaredRole.PACKING_LIST, pages, locales or {n: None for n in pages})


def _item(seq, desc, qty="1", total="100"):
    return WorkItem(
        xml_item_sequence=seq, source_invoice_number="INV-1", source_invoice_date="",
        source_invoice_item_index=seq, source_invoice_item_no=None,
        description_raw=desc, quantity=D(qty), invoice_uom_raw="PCS",
        unit_price=D("1"), line_total=D(total), currency="USD")


def _payloads(*rowsets):
    return [PackingListChunkRaw.model_validate({
        "role_validation": {"expected_role": "PACKING_LIST", "matches_expected_role": True},
        "rows": [{"source_page_no": 1, "source_row_index": i + 1, **r}
                 for i, r in enumerate(rows)],
    }) for rows in rowsets]


# --------------------------------------------------------------------------- #
# [0] a goods row is not a header, however many header words it contains
# --------------------------------------------------------------------------- #
GOODS_PAGE = "\n".join([
    "| C/NO | DESCRIPTION OF GOODS | QTY | UOM | N.W. (KGS) | G.W. (KGS) |",
    "| 1-5 | COTTON T-SHIRTS | 100 | PCS | 50.000 | 55.000 |",
    "| 6-10 | LEATHER GOODS | 50 | CTNS | 30.000 | 33.000 |",
    "| 11-15 | PLASTIC WARE | 80 | PCS | 20.000 | 22.000 |",
    "| TOTAL | | 230 | | 100.000 | 110.000 |",
])


def test_a_goods_row_containing_header_words_is_not_swallowed_as_a_header():
    """'LEATHER GOODS | 50 | CTNS' matched desc+ctn and became the column map:
    the row deleted itself, every later row lost its weights, and the page was
    still parser-owned with the totals gate disabled."""
    res = _parse({1: GOODS_PAGE})
    rows = res.pages[1].rows
    assert [r.description_raw for r in rows] == ["COTTON T-SHIRTS", "LEATHER GOODS", "PLASTIC WARE"]
    assert [r.gross_weight.value_raw for r in rows] == ["55.000", "33.000", "22.000"]
    assert res.printed_totals["gross_wt"][0] == "110.000"


def test_a_data_row_never_produces_a_header_map():
    assert _header_map(_cells("| 6-10 | LEATHER GOODS | 50 | CTNS | 30.000 | 33.000 |"),
                       need_qty=False) is None
    assert _header_map(_cells("| 2 | GENERAL GOODS ASSORTED | 100 | PCS | 2.00 | 200.00 |")) is None


# --------------------------------------------------------------------------- #
# [1] a per-carton rate column is not a carton count
# --------------------------------------------------------------------------- #
def test_units_per_carton_is_not_a_carton_count():
    res = _parse({1: "\n".join([
        "| C/NO | DESCRIPTION OF GOODS | UNITS PER CARTON | QTY | N.W. (KGS) | G.W. (KGS) |",
        "| 1-10 | SHAMPOO 500 ML | 120 | 1200 | 120.000 | 135.000 |",
        "| 11-15 | HAND WASH 250 ML | 120 | 600 | 30.000 | 35.000 |",
        "| TOTAL | | | 1800 | 150.000 | 170.000 |",
    ])})
    rows = res.pages[1].rows
    # the count comes from the C/NO ranges, not the 120-per-carton rate
    assert [r.carton_count.value_raw for r in rows] == ["10", "5"]
    assert [r.quantity_raw for r in rows] == ["1200", "600"]


@pytest.mark.parametrize("cell", ["PCS/CTN", "N.W./CTN (KGS)", "KG/PC", "WT PER PIECE"])
def test_rate_headers_map_to_no_body_column(cell):
    m = _header_map(_cells(f"| SL | DESCRIPTION | {cell} | G.W. (KGS) |"), need_qty=False)
    assert m is not None
    assert m.get("ctn") is None and m.get("net_wt") is None and m.get("qty") is None


def test_pack_size_header_is_a_size_not_a_carton_count():
    m = _header_map(_cells("| SL | DESCRIPTION | PACK SIZE | QTY | G.W. (KGS) |"))
    assert m is not None and m.get("ctn") is None


def test_qty_slash_unit_header_still_maps_quantity():
    """'Qty./ Unit' is a combined quantity+UOM header, not a per-unit rate —
    the first version of this guard unmapped it and lost the whole page."""
    m = _header_map(_cells("| Sn | Description | Corrugated Box No. | HSN | Qty./ Unit | Rate | Amount |"))
    assert m is not None and m["qty"] == 4


# --------------------------------------------------------------------------- #
# [2] TOTAL-prefixed and slash weight headers map; per-piece ones do not
# --------------------------------------------------------------------------- #
def test_total_prefixed_and_slash_weight_headers_are_mapped():
    res = _parse({1: "\n".join([
        "| SL | DESCRIPTION OF GOODS | QTY | CTNS | N.W./CTN (KGS) | G.W./CTN (KGS) "
        "| TOTAL N.W. (KGS) | TOTAL G.W. (KGS) |",
        "| 1 | WIDGET ALPHA | 1200 | 10 | 12.000 | 13.500 | 120.000 | 135.000 |",
        "| 2 | WIDGET BETA | 600 | 5 | 6.000 | 7.000 | 30.000 | 35.000 |",
        "| TOTAL | | 1800 | 15 | | | 150.000 | 170.000 |",
    ])})
    rows = res.pages[1].rows
    assert [r.gross_weight.value_raw for r in rows] == ["135.000", "35.000"]
    assert [r.net_weight.value_raw for r in rows] == ["120.000", "30.000"]
    assert [r.carton_count.value_raw for r in rows] == ["10", "5"]


def test_slash_form_weight_headers_are_mapped():
    res = _parse({1: "\n".join([
        "| SL | DESCRIPTION | QTY | N/W (KGS) | G/W (KGS) |",
        "| 1 | WIDGET ALPHA | 10 | 6.000 | 7.500 |",
    ])})
    r = res.pages[1].rows[0]
    assert r.net_weight.value_raw == "6.000" and r.gross_weight.value_raw == "7.500"


def test_per_piece_weight_header_is_never_the_row_total():
    m = _header_map(_cells("| SL | DESCRIPTION | QTY | NET WT PER PIECE (KGS) | TOTAL NET WT (KGS) |"))
    assert m is not None and m.get("net_wt") == 4          # the TOTAL column, not the rate


# --------------------------------------------------------------------------- #
# [3] counts derived from carton ranges are never serials
# --------------------------------------------------------------------------- #
def test_range_derived_carton_counts_survive_the_serial_heuristic():
    res = _parse({1: "\n".join([
        "| C/NO | DESCRIPTION OF GOODS | G.W. (KGS) |",
        "| 1-2 | WIDGET ALPHA | 60.000 |",
        "| 3-5 | WIDGET BETA | 30.000 |",
        "| 6-9 | WIDGET GAMMA | 10.000 |",
    ])})
    rows = res.pages[1].rows
    assert [r.carton_count.value_raw for r in rows] == ["2", "3", "4"]
    assert not any("serial" in n for n in res.notes)


def test_a_true_serial_column_is_still_demoted():
    res = _parse({1: "\n".join([
        "| PKGS | DESCRIPTION | G.W. KGS |",
        "| 1 | WIDGET A | 10.000 |", "| 2 | WIDGET B | 10.000 |", "| 3 | WIDGET C | 10.000 |",
    ])})
    assert all(r.carton_count is None for r in res.pages[1].rows)


# --------------------------------------------------------------------------- #
# [4] one canonical group id — an OCR space must not split a shared group
# --------------------------------------------------------------------------- #
def test_ocr_spacing_variants_of_one_carton_range_stay_one_group():
    res = _parse({1: "\n".join([
        "| C/NO | DESCRIPTION | G.W. KGS |",
        "| 1-5 | WIDGET ALPHA | 10.000 |",
        "| 1 - 5 | WIDGET BETA | 6.000 |",
    ])})
    rows = res.pages[1].rows
    assert rows[0].shared_carton_group_raw == rows[1].shared_carton_group_raw
    items = [_item(1, "WIDGET ALPHA"), _item(2, "WIDGET BETA")]
    payload = PackingListChunkRaw(
        role_validation=RoleValidation(expected_role=DeclaredRole.PACKING_LIST), rows=rows)
    ev = match_packing(items, [payload])
    assert ev[1].carton_count + ev[2].carton_count == D("5")     # never 10


# --------------------------------------------------------------------------- #
# [5] the printed total is parsed under the same locale as the rows
# --------------------------------------------------------------------------- #
def test_eu_format_totals_do_not_stand_the_parser_down():
    page = "\n".join([
        "| C/NO | DESCRIPTION OF GOODS | N.W. | G.W. |",
        "| 1 | WIDGET ALPHA | 6,000 KGS | 7,500 KGS |",
        "| 2 | WIDGET BETA | 4,000 KGS | 4,500 KGS |",
        "| TOTAL | | 10,000 KGS | 12,000 KGS |",
    ])
    res = _parse({1: page}, locales={1: "EU"})
    assert res.pages and res.pages[1].confirmed
    assert any("matches the printed total" in n for n in res.notes)
    assert not any("does not match" in n for n in res.notes)


# --------------------------------------------------------------------------- #
# [6] a measurement is not a product code, and code matches report themselves
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", ["HAND WASH LIQUID 500ML", "LED FLOODLIGHT 1000W",
                                  "CERAMIC FLOOR TILE 1200X600 MATT", "A4 PAPER 80GSM",
                                  "BATTERY 12V 100AH", "SHAMPOO 24X500ML CARTON"])
def test_measurements_are_not_product_codes(text):
    assert _codes(text) == frozenset()


def test_real_part_numbers_still_qualify_as_codes():
    assert "ronyx22515x" in _codes("STENT RONYX22515X")
    assert "01e3120" in _codes("01E3120")


def test_a_shared_pack_size_no_longer_pairs_unrelated_products():
    items = [_item(1, "DISHWASH LIQUID 500ML"), _item(2, "FLOOR CLEANER 1 LTR")]
    warnings = []
    ev = match_packing(items, _payloads([
        {"description_raw": "FLOOR CLEANING LIQUID", "item_code_raw": "500ML",
         "gross_weight": {"value_raw": "9", "unit_raw": "KG"}},
    ]), warnings_out=warnings)
    assert ev[1].gross_weight is None                       # dishwash got nothing
    codes = {w.code for w in warnings}
    assert "PACKING_MATCH_BY_CODE" not in codes


def test_a_code_match_reports_itself():
    items = [_item(1, "CORONARY STENT SYSTEM 2.25X15")]
    items[0].model_raw = "RONYX22515X"
    warnings = []
    ev = match_packing(items, _payloads([
        {"description_raw": "STENT ONYX FRONTIER RX", "item_code_raw": "RONYX22515X",
         "gross_weight": {"value_raw": "3.2", "unit_raw": "KG"}},
    ]), warnings_out=warnings)
    assert ev[1].gross_weight == D("3.2")
    assert any(w.code == "PACKING_MATCH_BY_CODE" and "SN 1" in w.message for w in warnings)


# --------------------------------------------------------------------------- #
# [7] a purchase order is not a carton range
# --------------------------------------------------------------------------- #
def test_a_po_number_group_id_never_overrides_printed_carton_counts():
    items = [_item(1, "WIDGET ALPHA"), _item(2, "WIDGET BETA")]
    ev = match_packing(items, _payloads([
        {"description_raw": "WIDGET ALPHA", "carton_count": {"value_raw": "3"},
         "shared_carton_group_raw": "PO-1001-2024"},
        {"description_raw": "WIDGET BETA", "carton_count": {"value_raw": "2"},
         "shared_carton_group_raw": "PO-1001-2024"},
    ]))
    assert ev[1].carton_count + ev[2].carton_count == D("5")     # 3 + 2, never 1024


def test_a_pure_carton_range_id_still_settles_the_repeated_value_case():
    items = [_item(1, "A1"), _item(2, "A2"), _item(3, "A3")]
    ev = match_packing(items, _payloads([
        {"description_raw": "A1", "carton_count": {"value_raw": "1"}, "shared_carton_group_raw": "1-5"},
        {"description_raw": "A2", "carton_count": {"value_raw": "1"}, "shared_carton_group_raw": "1-5"},
        {"description_raw": "A3", "carton_count": {"value_raw": "1"}, "shared_carton_group_raw": "1-5"},
    ]))
    assert ev[1].carton_count + ev[2].carton_count + ev[3].carton_count == D("5")


def test_differing_printed_values_beat_even_a_pure_range_id():
    """Rows that print different values already carry their own split — the sum
    is the document's own statement and nothing may override it."""
    items = [_item(1, "A1"), _item(2, "A2")]
    ev = match_packing(items, _payloads([
        {"description_raw": "A1", "carton_count": {"value_raw": "0.6"}, "shared_carton_group_raw": "1-2"},
        {"description_raw": "A2", "carton_count": {"value_raw": "1.4"}, "shared_carton_group_raw": "1-2"},
    ]))
    assert ev[1].carton_count + ev[2].carton_count == D("2.0")


# --------------------------------------------------------------------------- #
# [8] carton numbering restarts per document
# --------------------------------------------------------------------------- #
def test_identical_group_ids_in_two_documents_are_two_groups():
    items = [_item(i, d) for i, d in enumerate(["ALPHA", "BRAVO", "CHARLIE", "DELTA"], start=1)]
    doc_a = [{"description_raw": d, "quantity_raw": "10",
              "carton_count": {"value_raw": "1"}, "shared_carton_group_raw": "1-5"}
             for d in ("ALPHA", "BRAVO")]
    doc_b = [{"description_raw": d, "quantity_raw": "10",
              "carton_count": {"value_raw": "1"}, "shared_carton_group_raw": "1-5"}
             for d in ("CHARLIE", "DELTA")]
    ev = match_packing(items, _payloads(doc_a, doc_b))
    total = sum(ev[i].carton_count for i in (1, 2, 3, 4))
    assert total == D("10")                                  # 5 per document, never 5 shared


# --------------------------------------------------------------------------- #
# [9]-[15] description conversion
# --------------------------------------------------------------------------- #
def _net(desc, qty="1", uom="CTN"):
    r = net_from_description(desc, D(qty), uom)
    return None if r is None else r.net_kg


def test_dozen_multiplier_never_lands_on_a_package_weight():
    r = net_from_description("MEN'S COTTON T-SHIRT, 1 DOZEN PER POLYBAG, CARTON NET 6 KG",
                             D("100"), "CTN")
    assert r.net_kg == D("600")                              # was 7200
    assert any("dozen" in w for w in r.warnings)


def test_carton_dimensions_do_not_steal_the_pack_binding():
    assert _net("INSTANT COFFEE, MASTER CARTON 40 X 30 X 20 CM, 12 X 1 KG POUCHES",
                "50") == D("600")                            # was 50


def test_nested_pack_chain_multiplies_through():
    assert _net("MINERAL WATER 4 X 6 X 1.5 LTR SHRINK PACK", "10") == D("360.00")


def test_space_thousands_separator_is_read_whole():
    r = net_from_description("SODA ASH DENSE, NET WEIGHT 1 250 KG", D("1"), "CTN")
    assert r.net_kg == D("1250") and r.confidence == "LOW"   # was 250, silently


def test_a_zero_reading_is_never_a_weight():
    r = net_from_description("COOKING OIL 1 000 ML BOTTLE", D("1"), "CTN")
    assert r is not None and r.net_kg == D("0.92")           # was 0 kg at HIGH


@pytest.mark.parametrize("desc,expected", [
    ("REFINED COOKING OIL 1 LTR X 12 PER CARTON", D("110.40")),
    ("REFINED COOKING OIL 12 X 1 LTR PER CARTON", D("110.40")),
    ("OLIVE OIL 500 ML X 24", D("109.20")),
    ("COOKING OIL 12/1LTR", D("110.40")),
])
def test_reversed_and_slash_pack_notations_carry_the_multiplier(desc, expected):
    assert _net(desc, "10") == expected


@pytest.mark.parametrize("desc", [
    "SUBMERSIBLE WATER PUMP 1 HP, 100 LTR/MIN",              # a flow rate
    "PLASTIC WATER TANK 1000 LTR TRIPLE LAYER",              # a capacity
    "JUICE DISPENSER 20 LTR CAPACITY",
    "RICE BAG 25-50 KG",                                     # a size range
    "JERRY CAN 20-25 LTR WATER",
    "PET BOTTLES 500-1000 ML JUICE",
])
def test_rates_capacities_and_ranges_are_refused(desc):
    assert net_from_description(desc, D("10"), "PCS") is None


def test_a_distant_gross_marker_still_flags_a_package_weight():
    r = net_from_description(
        "TOTAL GROSS WEIGHT OF THE CONSIGNMENT INCLUDING PACKING 1250 KG", D("1"), "CTN")
    assert r.confidence == "LOW"
    assert any("shipping package" in w for w in r.warnings)


def test_a_bare_trailing_weight_clause_borrows_its_context():
    """'…MASTER CARTON WITH INNER POLYBAG, 15 KG' — the 15 kg is the carton's,
    and the words saying so are on the other side of the comma."""
    assert _net("SHAMPOO 500 ML BOTTLE PACKED IN EXPORT MASTER CARTON WITH INNER POLYBAG, 15 KG",
                "10") == D("5.000")                          # the 500 ML content, was 150


# --------------------------------------------------------------------------- #
# end-to-end: finding [1]'s full declaration path
# --------------------------------------------------------------------------- #
def test_units_per_carton_page_allocates_the_printed_cartons():
    res = _parse({1: "\n".join([
        "| C/NO | DESCRIPTION OF GOODS | UNITS PER CARTON | QTY | N.W. (KGS) | G.W. (KGS) |",
        "| 1-10 | SHAMPOO 500 ML | 120 | 1200 | 120.000 | 135.000 |",
        "| 11-15 | HAND WASH 250 ML | 120 | 600 | 30.000 | 35.000 |",
        "| TOTAL | | | 1800 | 150.000 | 170.000 |",
    ])})
    payload = PackingListChunkRaw(
        role_validation=RoleValidation(expected_role=DeclaredRole.PACKING_LIST),
        rows=res.pages[1].rows)
    items = [_item(1, "SHAMPOO 500 ML", qty="1200"), _item(2, "HAND WASH 250 ML", qty="600")]
    ev = match_packing(items, [payload])
    allocate_weights_and_cartons(items, ev, D("170"), D("15"))
    assert [i.package_count for i in items] == [D("10.00"), D("5.00")]   # was 7.50/7.50
