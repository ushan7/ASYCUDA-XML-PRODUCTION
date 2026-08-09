"""A borrowed vendor layout must never silently re-map another vendor's columns.

2026-08-03 live job (P3S INTERNATIONAL, invoice PTIL455, 15 rows, $36,058.40).
The OCR was perfect.  Everything after it went wrong in a single chain:

1. the description header printed the SINGULAR "PARTICULAR"; `_HDR_KEYS["desc"]`
   only knew "PARTICULARS", so a header whose every other column matched was
   rejected wholesale and the document counted as headerless;
2. with no header, the parser fell back to vendor layout memory and applied
   another vendor's SIX-column map to this SEVEN-column table — H.S CODE read as
   the quantity, QTY read as the unit, UNIT read as the rate, RATE read as the
   line total, TOTAL dropped;
3. the arithmetic gate that is supposed to make a wrong remembered layout
   harmless never ran: the rate cell held the unit word "KGM", parsed to None,
   and the `price is None` branch confirmed the row UNCHECKED.  15 rows shipped
   claiming 48-billion-piece quantities worth $0.75;
4. the UOM cell held "90", which is not a unit word, so the parser fell back to
   its hardcoded "PCS" — every row asserted PCS against an invoice printing
   KGM / PRS / MTR, and nothing downstream could tell an assumed unit from a
   printed one;
5. "PRS" was missing from the UOM vocabulary entirely, so the three footwear
   rows produced no goods-row anchor and a phantom EXTRACTION_OVERCOUNT told
   the reviewer to hunt duplicates that did not exist;
6. the wrong map was then RECORDED back into memory under the signature
   "POSITIONAL" with this vendor's name and a 15-row score — a self-confirming
   entry keyed to match every future headerless invoice.

These tests pin each layer independently, so fixing one can never quietly
re-open another.
"""
import pytest

from app.domain.enums import DeclaredRole
from app.extraction.layout_memory import record_layout, stored_layouts
from app.extraction.manifest import anchor_count
from app.extraction.table_parser import _cells, _confirm_invoice_row, _header_map, parse_pages

# The live invoice table, verbatim from the OCR markdown.
HEADER = "|  S.NO | PARTICULAR | H.S CODE | QTY | UNIT | RATE $ | TOTAL(USD)  |"
SEP = "| --- | --- | --- | --- | --- | --- | --- |"
ROWS = [
    "|  1 | PAPER CARTOON BOX | 48192000000 | 90 | KGM | 0.75 | 67.50  |",
    "|  2 | PRINTER PARTS HOT ROLLER | 84439100000 | 560 | PCS | 0.15 | 84.00  |",
    "|  3 | PRINTER PARTS PRESSER ROLLER | 84439100000 | 100 | PCS | 0.15 | 15.00  |",
    "|  4 | SCHOOL BAG | 42022200000 | 512 | PCS | 3.00 | 1536.00  |",
    "|  5 | FLAT CONVERSE SHOES | 64059000000 | 1200 | PRS | 5.00 | 6000.00  |",
    "|  6 | DRESS MAKING CLOTH | 52094200000 | 12956 | MTR | 0.90 | 11660.40  |",
    "|  7 | KEYBOARD | 84716010000 | 3000 | PCS | 1.00 | 3000.00  |",
    "|  8 | FLAT SPORT SHOES | 64059000000 | 600 | PRS | 8.00 | 4800.00  |",
    "|  9 | LADIES BAG | 42022200000 | 230 | PCS | 3.00 | 690.00  |",
    "|  10 | MOUSE | 84716030000 | 1950 | PCS | 0.50 | 975.00  |",
    "|  11 | FUR CLOTH | 55141900000 | 597 | KGM | 2.50 | 1492.50  |",
    "|  12 | SPORT SHOES | 64059000000 | 471 | PRS | 8.00 | 3768.00  |",
    "|  13 | PU SYNTHETIC FEBRIC | 39219090000 | 720 | KGM | 1.00 | 720.00  |",
    "|  14 | SMALL LAMINATION MACHINE | 84393000000 | 100 | PCS | 10.00 | 1000.00  |",
    "|  15 | SPEAKER | 85182900000 | 25 | PCS | 10.00 | 250.00  |",
]
TOTALS = [
    "|  TOTAL USD |   |   |   |   |  | 36,058.40  |",
    "|  TRADE DISCOUNT USD |   |   |   |   |  | 58.40  |",
    "|  TOTAL USD VALUE |   |   |   |   |  | $36,000.00  |",
]
PAGE = "\n".join([HEADER, SEP, *ROWS, *TOTALS])

# the map the parser borrowed: six columns, learned from a different vendor
BORROWED = {"model": 0, "desc": 1, "qty": 2, "uom": 3, "price": 4, "total": 5, "n_cols": 6}


def _parse(page=PAGE, fallbacks=()):
    return parse_pages(DeclaredRole.INVOICE, {1: page}, {1: None},
                       fallback_mappings=fallbacks)


# --- layer 1: the header must be readable ---------------------------------- #

def test_singular_particular_is_a_description_header():
    """"PARTICULAR" is the standard description header on Nepali/Indian trade
    invoices; requiring the plural rejected an otherwise perfect header."""
    mapping = _header_map(_cells(HEADER))
    assert mapping is not None, "a fully-readable header was rejected"
    assert mapping["desc"] == 1
    # every other column was already correct — the whole map died on `desc`
    assert (mapping["line_no"], mapping["hs"], mapping["qty"]) == (0, 2, 3)
    assert (mapping["uom"], mapping["price"], mapping["total"]) == (4, 5, 6)


def test_document_parses_from_its_own_header_not_from_memory():
    res = _parse(fallbacks=[BORROWED])
    assert res.from_memory is False
    assert res.header_signature is not None
    assert res.confirmed_row_count() == 15


@pytest.mark.parametrize("row_line,qty,uom,price,total,hs", [
    (ROWS[0], "90", "KGM", "0.75", "67.50", "48192000000"),
    (ROWS[5], "12956", "MTR", "0.90", "11660.40", "52094200000"),
    (ROWS[4], "1200", "PRS", "5.00", "6000.00", "64059000000"),
])
def test_every_column_lands_in_its_own_field(row_line, qty, uom, price, total, hs):
    rows = _parse().pages[1].rows
    row = next(r for r in rows if r.evidence[0].quote.strip() == row_line.strip())
    assert (row.quantity_raw, row.uom_raw) == (qty, uom)
    assert (row.unit_price_raw, row.line_total_raw) == (price, total)
    assert row.hs_code_raw == hs, "the invoice PRINTS the HS code — never guess it"


def test_line_values_sum_to_the_printed_invoice_total():
    from decimal import Decimal

    rows = _parse().pages[1].rows
    assert sum(Decimal(r.line_total_raw.replace(",", "")) for r in rows) == Decimal("36058.40")


# --- layer 2: a remembered layout may not be applied to a wider table ------- #

def test_remembered_layout_is_refused_on_a_different_column_count():
    """`n_cols` was recorded from day one and never read: a 6-column map ran
    over a 7-column table and shifted every column after the description."""
    headerless = "\n".join([*ROWS, *TOTALS])          # header removed: bad scan
    res = _parse(page=headerless, fallbacks=[BORROWED])
    assert res.pages == {}, "a 6-column map must not parse a 7-column table"


def test_matching_width_still_lets_a_remembered_layout_work():
    """The width guard must not disable layout memory itself."""
    six_col = [
        "|  M1 | KEYBOARD | 3000 | PCS | 1.00 | 3000.00  |",
        "|  M2 | MOUSE | 1950 | PCS | 0.50 | 975.00  |",
        "|  M3 | SPEAKER | 25 | PCS | 10.00 | 250.00  |",
    ]
    res = _parse(page="\n".join(six_col), fallbacks=[BORROWED])
    assert res.from_memory is True
    assert res.confirmed_row_count() == 3


# --- layer 3: the arithmetic gate a borrowed layout rests on ---------------- #

def test_row_is_not_confirmed_when_the_rate_cell_is_not_a_number():
    """The documented safety net — qty x price == total — silently did not run
    when the price cell failed to parse, which is EXACTLY what a shifted column
    map produces.  Strict mode is on whenever the map came from memory."""
    cells = _cells(ROWS[0])
    lenient = _confirm_invoice_row(cells, BORROWED, 1, 1, ROWS[0], None)
    assert lenient is not None and lenient.quantity_raw == "48192000000", (
        "pinning the old behaviour: without strict mode the shifted row still confirms")

    strict = _confirm_invoice_row(cells, BORROWED, 1, 1, ROWS[0], None,
                                  strict_arithmetic=True)
    assert strict is None, "a borrowed map must prove qty x rate == amount"


def test_amount_only_rows_still_confirm_under_a_documents_own_header():
    """Strictness applies to BORROWED maps only: invoices that print an amount
    and no per-unit rate are routine and must keep parsing."""
    page = "\n".join([
        "|  SN | DESCRIPTION | QTY | UNIT | AMOUNT  |",
        "| --- | --- | --- | --- | --- |",
        "|  1 | KEYBOARD | 100 | PCS | 300.00  |",
        "|  2 | MOUSE | 50 | PCS | 150.00  |",
        "|  TOTAL AMOUNT |   |   |   | 450.00  |",
    ])
    res = _parse(page=page)
    assert res.confirmed_row_count() == 2
    assert [r.line_total_raw for r in res.pages[1].rows] == ["300.00", "150.00"]


# --- layer 4: an unreadable unit is a blank field, never an assumed one ------ #

def test_unreadable_uom_cell_stays_empty_rather_than_defaulting_to_pcs():
    cells = _cells(ROWS[0])
    # a 7-column map whose UOM slot lands on the QTY column (a number, not a unit)
    shifted = {"line_no": 0, "desc": 1, "qty": 2, "uom": 3, "total": 6, "n_cols": 7}
    row = _confirm_invoice_row(cells, shifted, 1, 1, ROWS[0], None)
    assert row is not None
    assert row.uom_raw is None, "an absent unit is a question for the reviewer, not 'PCS'"


# --- layer 5: PRS is a unit ------------------------------------------------- #

def test_prs_counts_as_a_goods_row_anchor():
    """Three footwear rows produced no anchor, so a 15-row page counted 12 and
    raised a phantom EXTRACTION_OVERCOUNT against a correct extraction."""
    assert anchor_count({1: PAGE}) == {1: 15}


# --- layer 6: the invoice's own printed total is the backstop --------------- #

def test_parse_stands_down_when_line_values_miss_every_printed_total():
    """The structural catch: each row is individually plausible, and only the
    SUM can see that the document is off by three orders of magnitude.  The
    invoice states its own sum, so the parser can check itself — this is the
    layer that would have caught the live job even if every other fix failed."""
    page = "\n".join([
        "|  SN | DESCRIPTION | QTY | UNIT | RATE | AMOUNT  |",
        "| --- | --- | --- | --- | --- | --- |",
        "|  1 | KEYBOARD | 100 | PCS | 3.00 | 300.00  |",
        "|  2 | MOUSE | 50 | PCS | 3.00 | 150.00  |",
        "|  TOTAL USD |   |   |   |   | 36,058.40  |",
    ])
    res = _parse(page=page)
    assert res.pages == {}, "a parse that misses the invoice's own total must stand down"
    assert any("matches none of the printed invoice totals" in n for n in res.notes)


def test_borrowed_layout_of_matching_width_still_dies_on_the_arithmetic():
    """Belt and braces: even with the width guard defeated, the shifted rows
    never confirm, so the parse stands down before it can reach the reviewer."""
    headerless = "\n".join([*ROWS, *TOTALS])
    six_col_wide = dict(BORROWED, n_cols=7)          # defeat the width guard
    assert _parse(page=headerless, fallbacks=[six_col_wide]).pages == {}


def test_a_discounted_invoice_matches_its_subtotal_not_only_its_net_total():
    """This invoice prints BOTH 36,058.40 (goods) and 36,000.00 (net of a 58.40
    discount).  A gate that insisted on the last printed figure would stand
    down on a perfectly correct parse."""
    res = _parse()
    assert res.confirmed_row_count() == 15
    assert any("matches the printed invoice total" in n for n in res.notes)


def test_no_printed_total_leaves_the_parse_alone():
    page = "\n".join([HEADER, SEP, *ROWS])
    res = _parse(page=page)
    assert res.confirmed_row_count() == 15
    assert any("prints no totals row" in n for n in res.notes)


# --- layer 7: memory must not confirm itself -------------------------------- #

def test_a_layout_without_a_header_signature_is_never_recorded(tmp_path, monkeypatch):
    """A parse with no signature CAME from the store; storing it again scores a
    borrowed map on the rows it got wrong and keys it to match every headerless
    document of the role."""
    from app import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "storage_dir", tmp_path, raising=False)
    config.get_settings.cache_clear()
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr("app.extraction.layout_memory.get_settings", lambda: settings)

    record_layout(DeclaredRole.INVOICE, BORROWED, None, "P3S INTERNATIONAL LIMITED", 15)
    assert stored_layouts(DeclaredRole.INVOICE) == []

    record_layout(DeclaredRole.INVOICE, BORROWED, "MODELNO|DESCRIPTION|QTY|UM|PRICE|TOTAL",
                  "Some Vendor", 15)
    assert stored_layouts(DeclaredRole.INVOICE) == [BORROWED]


def test_positional_entries_already_in_the_store_are_retired(tmp_path, monkeypatch):
    import json

    from app import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "storage_dir", tmp_path, raising=False)
    monkeypatch.setattr("app.extraction.layout_memory.get_settings", lambda: settings)
    (tmp_path / "vendor_layouts.json").write_text(json.dumps({
        "version": 1,
        "layouts": [{"role": "INVOICE", "header_signature": "POSITIONAL",
                     "mapping": BORROWED, "confirmed_rows": 15, "docs": 2}],
    }), encoding="utf-8")
    assert stored_layouts(DeclaredRole.INVOICE) == []
