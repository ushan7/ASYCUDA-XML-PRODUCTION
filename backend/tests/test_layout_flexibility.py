"""Layout flexibility (2026-07-18): the deterministic fast path must recognize
many vendor/industry invoice patterns, not just the ones tuned against — and
must safely STAND DOWN (→ LLM path) on layouts it cannot confirm.

Every confirmed row is still arithmetic-proven (qty x price == total); these
tests vary column order, header labels, UOM vocabulary, HS/COO placement and
language so the fast path generalizes.
"""
from decimal import Decimal

from app.domain.enums import DeclaredRole
from app.extraction.manifest import goods_row_anchors, qty_uom_cell_at
from app.extraction.openai_extractor import _header_zone_pages
from app.extraction.table_parser import parse_pages
from app.extraction.validator import _QTY_UOM_CELLS
from app.ocr.base import OcrPage


def _parse(md: str):
    res = parse_pages(DeclaredRole.INVOICE, {1: md}, {1: "US"})
    return res.pages.get(1)


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


# --------------------------------------------------------------------------- #
# column order / label variety — all must confirm by arithmetic
# --------------------------------------------------------------------------- #
def test_part_number_first_column_order():
    md = "\n".join([
        "| Item No | Part | Description | Qty | UOM | Unit Price | Amount |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        "| 1 | ABC-100 | WIDGET BLUE | 4 | PCS | 25.00 | 100.00 |",
    ])
    pp = _parse(md)
    assert pp.confirmed and len(pp.rows) == 1
    r = pp.rows[0]
    assert r.model_raw == "ABC-100" and r.description_raw == "WIDGET BLUE"
    assert r.line_no_raw == "1" and (r.quantity_raw, r.uom_raw) == ("4", "PCS")


def test_indian_gst_particulars_hsn_rate_amount():
    md = "\n".join([
        "| Sl | Particulars | HSN | Qty | UOM | Rate | Amount |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        "| 1 | COTTON SHIRT | 62052000 | 10 | PCS | 5.00 | 50.00 |",
    ])
    pp = _parse(md)
    assert pp.confirmed and len(pp.rows) == 1
    r = pp.rows[0]
    assert r.description_raw == "COTTON SHIRT"          # "Particulars" -> desc
    assert r.hs_code_raw == "62052000"                  # "HSN" column captured
    assert (r.unit_price_raw, r.line_total_raw) == ("5.00", "50.00")


def test_unusual_uom_vocabulary_pairs_and_rolls():
    md = "\n".join([
        "| Description | Qty | Unit | Price | Total |",
        "| --- | --- | --- | --- | --- |",
        "| LEATHER SHOES | 3 | PAIRS | 20.00 | 60.00 |",
        "| COTTON FABRIC | 2 | ROLLS | 50.00 | 100.00 |",
    ])
    pp = _parse(md)
    assert pp.confirmed and len(pp.rows) == 2
    assert [r.uom_raw.upper() for r in pp.rows] == ["PAIRS", "ROLLS"]


def test_hs_and_origin_columns_after_the_money_columns():
    md = "\n".join([
        "| No | Description | Qty | UM | Price | Total | HS Code | Origin |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        "| 1 | GADGET | 5 | EA | 10.00 | 50.00 | 8501.10.00 | CN |",
    ])
    pp = _parse(md)
    assert pp.confirmed and len(pp.rows) == 1
    r = pp.rows[0]
    assert r.hs_code_raw == "8501.10.00"                # column sits AFTER total
    assert r.country_of_origin_raw == "CN"


def test_arithmetic_gate_still_rejects_wrong_rows_on_any_layout():
    md = "\n".join([
        "| Sl | Particulars | HSN | Qty | UOM | Rate | Amount |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        "| 1 | GOOD ROW | 62052000 | 10 | PCS | 5.00 | 50.00 |",
        "| 2 | BAD MATH | 62052000 | 10 | PCS | 5.00 | 99.99 |",
    ])
    pp = _parse(md)
    assert not pp.confirmed                              # page disowned to the LLM
    assert pp.suspicious_leftover >= 1                   # the bad row is flagged


# --------------------------------------------------------------------------- #
# safe stand-down: no recognizable header anywhere -> LLM path
# --------------------------------------------------------------------------- #
def test_headerless_document_stands_down():
    md = "\n".join([
        "| ABC-1 | random thing | 4 | PCS | 25.00 | 100.00 |",
        "| ABC-2 | another thing | 2 | PCS | 10.00 | 20.00 |",
    ])
    res = parse_pages(DeclaredRole.INVOICE, {1: md}, {1: "US"})
    assert res.pages == {}                               # stood down; historical LLM path runs


# --------------------------------------------------------------------------- #
# shared UOM vocabulary is consistent across manifest / validator / parser
# --------------------------------------------------------------------------- #
def test_uom_vocabulary_shared_across_layers():
    line = "| WIDGET | 3 | PAIRS | 20.00 | 60.00 |"
    # manifest cell locator sees the qty/UOM
    assert qty_uom_cell_at(_cells(line)) is not None
    # validator's page-level completeness check sees the same qty|UOM pair
    assert _QTY_UOM_CELLS.search(line) is not None
    # a merged "3 PAIRS (1/PR)" cell is recognized too
    merged = "| WIDGET | 3 PAIRS (1/PR) | 20.00 | 60.00 |"
    assert qty_uom_cell_at(_cells(merged)) is not None


def test_manifest_anchors_generic_part_and_gtin_tokens():
    # a GTIN-anchored row and an alphanumeric-part row both anchor
    assert goods_row_anchors(1, "| 09501101020917 | THING | 5 | EA |")
    assert goods_row_anchors(1, "| SKU-778A | THING | 5 | PCS |")
    # a header row never anchors (no qty|UOM data cells / identity)
    assert not goods_row_anchors(1, "| Sl | Description | Qty | UOM | Rate | Amount |")


# --------------------------------------------------------------------------- #
# content-aware header zone: adapts to header-deep / totals-high layouts
# --------------------------------------------------------------------------- #
def test_header_zone_keeps_header_deep_and_totals_high():
    first = OcrPage(page_no=1, plain_text="letterhead\nInvoice No: INV-1")
    filler = ["row %d widget 5 EA 2.00 10.00" % i for i in range(30)]
    mid_lines = ["TOTAL: 500.00"] + filler[:12] + \
                ["Invoice No: INV-2  Consignee: ACME  Currency: USD"] + filler[12:]
    middle = OcrPage(page_no=2, plain_text="\n".join(mid_lines))
    last = OcrPage(page_no=3, plain_text="Subtotal 500.00\nGrand Total 500.00")

    zoned = _header_zone_pages([first, middle, last], margin=3, ctx=1)
    mid_text = zoned[1].plain_text
    assert "Invoice No: INV-2" in mid_text               # header deep in the page kept
    assert "TOTAL: 500.00" in mid_text                   # totals near the top kept
    assert "[... omitted ...]" in mid_text               # goods bulk dropped
    assert len(mid_text.splitlines()) < len(mid_lines)   # actually trimmed
    # first and last pages are always whole
    assert zoned[0].plain_text == first.plain_text
    assert zoned[2].plain_text == last.plain_text
