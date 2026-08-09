"""Regression tests for the 2026-08-01 over-count / value-corruption audit.

Live job 1111a729 (a 9-page description-only surgical-instruments invoice,
Sn 1-184) declared 204 items: the page-5 LLM window re-emitted its context
page's 20 rows and the merge kept them; the over-count gate had zero
GTIN/part anchors to compare against; the OCR lost the value columns on
three pages, so 43 items shipped with a 0 value and 22 more with line totals
copied row-for-row from the neighbouring page; and the parser read a
column-shifted page's IGST-rate column (a constant 5.00) as every line's
total.  These tests pin the fixes:

* windows' out-of-scope rows are dropped at the merge;
* printed line numbers are a fallback anchor count;
* claimed money must be printed on the row's own page;
* a per-page constant/junk "taxable" column is re-based on the amount column;
* the extracted sum is checked against the document's printed totals, and a
  captured "grand total" that is a column subtotal is rejected;
* a page whose goods-table header lost its rightmost columns is flagged.
"""
from decimal import Decimal

import pytest

from app.domain.enums import DeclaredRole
from app.extraction.common_models import (
    Evidence, InvoiceChunkRaw, InvoiceLineRaw, InvoiceTotalsRaw, RoleValidation)
from app.extraction.manifest import anchor_count, serial_row_count
from app.extraction.openai_extractor import (
    _drop_out_of_scope_rows,
    flag_truncated_value_columns,
    ground_row_values,
    reconcile_invoice_sum,
    reconcile_row_duplicates,
)
from app.extraction.table_parser import parse_pages
from app.numbers import detect_numeric_locale

INV = DeclaredRole.INVOICE

# The live vendor's layout: a serial column, free-text descriptions (no
# GTIN/part token anywhere), merged qty+UOM cells, zero unit rates.
HDR = "|  Sn | Description of Goods | HSN/SAC | Qty./Unit | Rate In INR | Amount In INR  |"


def _line(sn, desc, qty, amount):
    return f"|  {sn} | {desc} | 9018 | {qty} | 0.0000 | {amount}  |"


def _row(page, ridx, desc, qty="1", total=None, price=None, hs=None):
    return InvoiceLineRaw(
        source_page_no=page, source_row_index=ridx, description_raw=desc,
        quantity_raw=qty, uom_raw="PC", unit_price_raw=price, line_total_raw=total,
        hs_code_raw=hs, evidence=[Evidence(page_no=page, quote=desc)])


class _P:
    def __init__(self, page_no, plain_text):
        self.page_no, self.plain_text = page_no, plain_text


def _payload(rows, totals=None):
    return InvoiceChunkRaw(
        role_validation=RoleValidation(expected_role=INV),
        rows=rows, totals=totals, page_numbers=sorted({r.source_page_no for r in rows}))


# --------------------------------------------------------------------------- #
# serial-number fallback anchors (description-only vendors)
# --------------------------------------------------------------------------- #
def test_serial_rows_are_counted_without_identity_tokens():
    page = "\n".join([HDR,
                      _line(56, "POTTS SCISSORS", "2PC", "1500.00"),
                      _line(57, "NEEDLE HOLDER 5' T.C", "2PC", "1700.00"),
                      "|  **Total** |   |   | **550.00** | **4200.00** | **3200.00**  |"])
    assert serial_row_count(page) == 2                 # header + totals never count
    assert anchor_count({1: page}) == {1: 2}


def test_overcount_gate_fires_on_serial_anchors():
    # the live failure: description-only rows produced ZERO token anchors and
    # the gate stood down while 20 duplicated rows shipped — serials now bound it
    page = "\n".join([HDR, _line(56, "POTTS SCISSORS", "2PC", "1500.00")])
    payload = _payload([_row(1, 1, "POTTS SCISSORS", total="1500.00"),
                        _row(1, 2, "POTTS SCISSORS", total="1500.00")])
    out, warnings = reconcile_row_duplicates(INV, [_P(1, page)], payload, [])
    assert len(out.rows) == 2                          # gate is read-only, always
    assert any(w.startswith("EXTRACTION_OVERCOUNT") for w in warnings)


def test_token_anchor_count_still_wins_when_larger():
    # token-anchored vendors are unchanged: serials only ever RAISE the bound
    hdr = "| MODEL NO | DESCRIPTION | QTY | U/M | UNIT PRICE | TOTAL |"
    line = "| RX55A1 | STENT RX55A1 | 3 | EA | 320.00 | 960.00 |"
    assert anchor_count({1: "\n".join([hdr, line])}) == {1: 1}


# --------------------------------------------------------------------------- #
# out-of-scope rows are dropped at the merge
# --------------------------------------------------------------------------- #
def test_out_of_scope_rows_are_dropped_with_warning():
    payload = _payload([_row(4, 1, "CONTEXT ROW", total="5.00"),
                        _row(5, 1, "IN-SCOPE ROW", total="950.00")])
    notes = _drop_out_of_scope_rows(payload, {5}, "the window for pages [5]")
    assert [r.source_page_no for r in payload.rows] == [5]
    assert len(notes) == 1 and notes[0].startswith("CONTEXT_ROWS_DROPPED")
    assert "[4]" in notes[0]


def test_in_scope_rows_pass_untouched():
    payload = _payload([_row(5, 1, "A", total="1.00"), _row(5, 2, "B", total="2.00")])
    assert _drop_out_of_scope_rows(payload, {5}, "w") == []
    assert len(payload.rows) == 2


# --------------------------------------------------------------------------- #
# value grounding: claimed money must be printed on the row's own page
# --------------------------------------------------------------------------- #
def test_unprinted_line_total_is_stripped():
    # the live failure: page 5's OCR printed no amounts, the LLM copied the
    # neighbouring page's value column row-for-row
    page = "\n".join([HDR, _line(76, "ARTERY FORCEP", "1PC", "")])
    payload = _payload([_row(5, 1, "ARTERY FORCEP", total="950.00", price="0.0000")])
    out, warnings = ground_row_values(INV, [_P(5, page)], payload, [])
    assert out.rows[0].line_total_raw is None          # 950.00 is printed nowhere
    assert out.rows[0].unit_price_raw == "0.0000"      # 0.0000 IS printed — kept
    assert any(w.startswith("ROW_VALUE_UNPRINTED") for w in warnings)


def test_printed_values_survive_formatting_differences():
    page = "\n".join([HDR, _line(1, "FORCEPS", "2PC", "1,500.00")])
    payload = _payload([_row(1, 1, "FORCEPS", total="1500.00")])   # LLM-normalized
    out, warnings = ground_row_values(INV, [_P(1, page)], payload, [])
    assert out.rows[0].line_total_raw == "1500.00" and warnings == []


def test_ocr_split_token_never_triggers_a_false_strip():
    # "1 500.00" OCR-split inside one line: the per-line digit run still proves it
    page = "\n".join([HDR, "|  1 | FORCEPS | 9018 | 2PC | 0.0000 | 1 500.00  |"])
    payload = _payload([_row(1, 1, "FORCEPS", total="1500.00")])
    out, warnings = ground_row_values(INV, [_P(1, page)], payload, [])
    assert out.rows[0].line_total_raw == "1500.00" and warnings == []


def test_grounding_only_applies_to_invoices():
    payload = _payload([_row(1, 1, "X", total="999.99")])
    out, warnings = ground_row_values(DeclaredRole.PACKING_LIST, [_P(1, "no table")], payload, [])
    assert out.rows[0].line_total_raw == "999.99" and warnings == []


# --------------------------------------------------------------------------- #
# HS grounding: the claimed tariff code must be printed on the row's own page
#
# hs_code_raw is priority 1 in the resolver and an 11-digit string that merely
# EXISTS in the official database is accepted as INVOICE_HS_EXACT at confidence
# 1.0 — no warning, no AUTO badge.  So an HS the document does not print must
# never survive: it sets the duty rate, and the review screen presents it as an
# invoice-printed fact that needs no second look.
# --------------------------------------------------------------------------- #
_HS_LINE = "|  1 | CATHETER SET | {hs} | 2PC | 750.00 | 1500.00  |"


def test_unprinted_hs_is_stripped():
    # the injection case: the page prints 9018…, the extraction claims 3004…
    # (a real code in the official DB, so nothing downstream would question it)
    page = "\n".join([HDR, _HS_LINE.format(hs="9018.90.90.900")])
    payload = _payload([_row(1, 1, "CATHETER SET", total="1500.00", hs="30049099000")])
    out, warnings = ground_row_values(INV, [_P(1, page)], payload, [])
    assert out.rows[0].hs_code_raw is None
    assert any(w.startswith("ROW_HS_UNPRINTED") for w in warnings)
    # the row itself survives — only the unverifiable claim is dropped
    assert out.rows[0].line_total_raw == "1500.00"


@pytest.mark.parametrize("printed", [
    "9018.90.90.900", "9018 90 90 900", "9018-90-90-900",
    "9018/90/90/900", "90189090900",
])
def test_printed_hs_survives_every_separator_style(printed):
    """The vendor's punctuation is not the extraction's fault: only the DIGITS
    are compared, via the per-line digit run."""
    page = "\n".join([HDR, _HS_LINE.format(hs=printed)])
    payload = _payload([_row(1, 1, "CATHETER SET", total="1500.00", hs="90189090900")])
    out, warnings = ground_row_values(INV, [_P(1, page)], payload, [])
    assert out.rows[0].hs_code_raw == "90189090900"
    assert not any(w.startswith("ROW_HS_UNPRINTED") for w in warnings)


def test_hs_printed_once_for_the_whole_page_is_kept():
    """Plenty of invoices print the tariff code in a header or a note rather
    than per row.  The digit index is page-wide, so that still counts."""
    page = "\n".join(["HS Code 9018.90.90.900 applies to all lines below", HDR,
                      "|  1 | CATHETER SET | | 2PC | 750.00 | 1500.00  |"])
    payload = _payload([_row(1, 1, "CATHETER SET", total="1500.00", hs="90189090900")])
    out, warnings = ground_row_values(INV, [_P(1, page)], payload, [])
    assert out.rows[0].hs_code_raw == "90189090900" and warnings == []


def test_hs_without_digits_is_left_alone():
    """"N/A" is not a claim to ground — the resolver already reads it as absent
    and moves to the next authority, so stripping it would only add noise."""
    page = "\n".join([HDR, _HS_LINE.format(hs="")])
    payload = _payload([_row(1, 1, "CATHETER SET", total="1500.00", hs="N/A")])
    out, warnings = ground_row_values(INV, [_P(1, page)], payload, [])
    assert out.rows[0].hs_code_raw == "N/A" and warnings == []


def test_hs_and_money_are_grounded_independently():
    """A row can have an honest HS and an invented total, or the reverse."""
    page = "\n".join([HDR, _HS_LINE.format(hs="9018.90.90.900")])
    payload = _payload([_row(1, 1, "CATHETER SET", total="8888.88", hs="90189090900")])
    out, warnings = ground_row_values(INV, [_P(1, page)], payload, [])
    assert out.rows[0].hs_code_raw == "90189090900"       # printed
    assert out.rows[0].line_total_raw is None             # not printed
    assert any(w.startswith("ROW_VALUE_UNPRINTED") for w in warnings)
    assert not any(w.startswith("ROW_HS_UNPRINTED") for w in warnings)


def test_hs_grounding_is_invoice_only():
    payload = _payload([_row(1, 1, "X", hs="30049099000")])
    out, warnings = ground_row_values(DeclaredRole.PACKING_LIST, [_P(1, "no table")], payload, [])
    assert out.rows[0].hs_code_raw == "30049099000" and warnings == []


# --------------------------------------------------------------------------- #
# per-page taxable-column rejection in the table parser
# --------------------------------------------------------------------------- #
_TAX_HDR = ("|  Sn | Description of Goods | HSN/SAC | Qty./Unit | Rate In INR | "
            "Amount In INR | Taxable AMT. IN INR  |")


def _parse(pages: dict[int, str]):
    locales = {n: detect_numeric_locale(t) for n, t in pages.items()}
    return parse_pages(INV, pages, locales)


def test_constant_taxable_column_is_rebased_per_page():
    # page 1: healthy — taxable varies and is the assessable value.  page 2:
    # the OCR shifted the IGST-rate column (constant 5.00) under "Taxable".
    p1 = "\n".join([_TAX_HDR,
                    "|  1 | FORCEPS A | 9018 | 2PC | 100.0000 | 200.00 | 220.00  |",
                    "|  2 | FORCEPS B | 9018 | 1PC | 300.0000 | 300.00 | 330.00  |",
                    "|  3 | FORCEPS C | 9018 | 2PC | 150.0000 | 300.00 | 340.00  |"])
    p2 = "\n".join(["|  4 | SCISSOR A | 9018 | 2PC | 0.0000 | 1500.00 | 5.00  |",
                    "|  5 | SCISSOR B | 9018 | 1PC | 0.0000 | 875.00 | 5.00  |",
                    "|  6 | SCISSOR C | 9018 | 4PC | 0.0000 | 3500.00 | 5.00  |"])
    res = _parse({1: p1, 2: p2})
    assert res.pages[1].confirmed and res.pages[2].confirmed
    assert [r.line_total_raw for r in res.pages[1].rows] == ["220.00", "330.00", "340.00"]
    assert [r.line_total_raw for r in res.pages[2].rows] == ["1500.00", "875.00", "3500.00"]
    assert any(n.startswith("p2: taxable/assessable column rejected") for n in res.notes)
    assert not any(n.startswith("p1:") for n in res.notes)


def test_varying_taxable_column_is_trusted():
    p1 = "\n".join([_TAX_HDR,
                    "|  1 | FORCEPS A | 9018 | 2PC | 100.0000 | 200.00 | 220.00  |",
                    "|  2 | FORCEPS B | 9018 | 1PC | 300.0000 | 300.00 | 330.00  |",
                    "|  3 | FORCEPS C | 9018 | 2PC | 150.0000 | 300.00 | 340.00  |"])
    res = _parse({1: p1})
    assert [r.line_total_raw for r in res.pages[1].rows] == ["220.00", "330.00", "340.00"]
    # No taxable-column rejection.  Not `notes == []`: the parser also records
    # whether it could cross-check its line values against a printed invoice
    # total, and this fixture prints no totals row.
    assert not any("taxable/assessable column rejected" in n for n in res.notes)


def test_mapped_qty_column_outranks_positional_size_cell():
    # "16 CM" in the box/size column reads as a merged quantity and sits BEFORE
    # the real qty column; with a zero rate no arithmetic can catch it — the
    # header-mapped column must win (live job: qty 16 declared for 6 pieces)
    hdr = ("|  Sn | Description of Goods | Corrugated Box No. | HSN/SAC | Qty./Unit | "
           "Rate In INR | Amount In INR  |")
    page = "\n".join([hdr, "|  59 | NEEDLE HOLDER | 16 CM | 9018 | 6PC | 0.0000 | 5250.00  |"])
    res = _parse({1: page})
    assert res.pages[1].confirmed
    (row,) = res.pages[1].rows
    assert row.quantity_raw == "6" and row.uom_raw == "PC"
    assert row.line_total_raw == "5250.00"


# --------------------------------------------------------------------------- #
# invoice sum gate + captured-total sanity
# --------------------------------------------------------------------------- #
def _sum_pages(total_line="|  **Total** |   |   |   |   | **1000.00**  |"):
    return [_P(1, "\n".join([HDR,
                             _line(1, "A", "1PC", "100.00"),
                             _line(2, "B", "1PC", "200.00"),
                             _line(3, "C", "1PC", "300.00"),
                             total_line]))]


def test_sum_shortfall_is_flagged():
    payload = _payload([_row(1, 1, "A", total="100.00"),
                        _row(1, 2, "B", total="200.00"),
                        _row(1, 3, "C", total="300.00")])
    out, warnings = reconcile_invoice_sum(INV, _sum_pages(), payload, [])
    assert any(w.startswith("INVOICE_SUM_MISMATCH") for w in warnings)
    assert any("600.00" in w and "1000.00" in w for w in warnings)


def test_matching_sum_stays_silent():
    payload = _payload([_row(1, 1, "A", total="100.00"),
                        _row(1, 2, "B", total="200.00"),
                        _row(1, 3, "C", total="300.00")])
    pages = _sum_pages("|  **Total** |   |   |   |   | **600.00**  |")
    out, warnings = reconcile_invoice_sum(INV, pages, payload, [])
    assert warnings == []


def test_larger_foreign_column_total_is_not_a_mismatch():
    # a totals row prints one total per column, and a rate column's total can
    # legitimately exceed the amount column's (live job 253-2: qty 2113,
    # rate-total 4926.66, amount-total 3202.33) — when ANY printed figure
    # matches the extracted sum the extraction is consistent
    payload = _payload([_row(1, 1, "A", total="100.00"),
                        _row(1, 2, "B", total="200.00"),
                        _row(1, 3, "C", total="300.00")])
    pages = _sum_pages("|  **Total** |   |   | **2113** | **4926.66** | **600.00**  |")
    out, warnings = reconcile_invoice_sum(INV, pages, payload, [])
    assert warnings == []


def test_column_subtotal_captured_as_grand_total_is_rejected():
    # the live failure: the rate column's 4200.00 was captured as "the" total
    # and produced a nonsense TOTAL_MISMATCH downstream
    payload = _payload([_row(1, 1, "A", total="100.00"),
                        _row(1, 2, "B", total="200.00"),
                        _row(1, 3, "C", total="300.00")],
                       totals=InvoiceTotalsRaw(grand_total_raw="42.00"))
    out, warnings = reconcile_invoice_sum(INV, _sum_pages(), payload, [])
    assert out.totals.grand_total_raw is None
    assert any(w.startswith("PRINTED_TOTAL_REJECTED") for w in warnings)


def test_multi_invoice_uploads_are_skipped():
    from app.extraction.common_models import SubInvoiceRaw
    payload = _payload([_row(1, 1, "A", total="100.00"),
                        _row(1, 2, "B", total="200.00"),
                        _row(1, 3, "C", total="300.00")])
    payload.sub_invoices = [SubInvoiceRaw(invoice_number_raw="X-1", first_page_no=1),
                            SubInvoiceRaw(invoice_number_raw="X-2", first_page_no=2)]
    out, warnings = reconcile_invoice_sum(INV, _sum_pages(), payload, [])
    assert warnings == []


# --------------------------------------------------------------------------- #
# OCR column-truncation detection
# --------------------------------------------------------------------------- #
_WIDE_HDR = ("|  Sn | Description of Goods | HSN/SAC | Qty./Unit | Rate In INR | "
             "Amount In INR | Taxable AMT. IN INR  |")
_NARROW_HDR = "|  Sn | Description of Goods | HSN/SAC | Qty./Unit | Rate In INR  |"


def _wide_page():
    return "\n".join([_WIDE_HDR,
                      "|  1 | FORCEPS A | 9018 | 2PC | 100.0000 | 200.00 | 220.00  |",
                      "|  2 | FORCEPS B | 9018 | 1PC | 300.0000 | 300.00 | 330.00  |",
                      "|  3 | FORCEPS C | 9018 | 2PC | 150.0000 | 300.00 | 340.00  |"])


def test_truncated_value_columns_are_flagged():
    # the live failure: pages 5/7/8 lost Rate/Amount/Taxable — rows end at 0.0000
    narrow = "\n".join([_NARROW_HDR,
                        "|  4 | SCISSOR A | 9018 | 1PC | 0.0000  |",
                        "|  5 | SCISSOR B | 9018 | 1PC | 0.0000  |",
                        "|  6 | SCISSOR C | 9018 | 1PC | 0.0000  |"])
    payload = _payload([_row(1, 1, "FORCEPS A", total="220.00")])
    out, warnings = flag_truncated_value_columns(
        INV, [_P(1, _wide_page()), _P(2, narrow)], payload, [])
    assert len(warnings) == 1 and warnings[0].startswith("PAGE_VALUE_COLUMNS_TRUNCATED")
    assert "page 2" in warnings[0] and "Amount In INR" in warnings[0]


def test_narrow_header_with_full_values_is_not_flagged():
    # a page whose header cells OCR-merged but whose rows still print amounts
    # (the live job's page 9) must not false-fire
    narrow_valued = "\n".join([_NARROW_HDR,
                               "|  4 | SCISSOR A | 9018 | 1PC | 0.0000 | 275.00  |",
                               "|  5 | SCISSOR B | 9018 | 1PC | 0.0000 | 320.00  |",
                               "|  6 | SCISSOR C | 9018 | 1PC | 0.0000 | 150.00  |"])
    payload = _payload([_row(1, 1, "FORCEPS A", total="220.00")])
    out, warnings = flag_truncated_value_columns(
        INV, [_P(1, _wide_page()), _P(2, narrow_valued)], payload, [])
    assert warnings == []


def test_uniform_width_document_is_not_flagged():
    payload = _payload([_row(1, 1, "FORCEPS A", total="220.00")])
    out, warnings = flag_truncated_value_columns(
        INV, [_P(1, _wide_page()), _P(2, _wide_page())], payload, [])
    assert warnings == []
