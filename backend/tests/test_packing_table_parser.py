"""Deterministic parsing of PACKING LISTS — the weight and carton columns.

Before these existed the parser emitted only line_no / description / quantity /
UOM for a packing row, and a parser-owned page never reaches the LLM.  Every
per-item gross weight and carton count printed on such a page was therefore
discarded, `have_gross` / `have_carton` were both False, and allocation fell
back to splitting the authorised gross by invoice VALUE on a shipment whose
packing list stated every weight.  Nothing in the suite called `parse_pages`
with `DeclaredRole.PACKING_LIST` at all.
"""
import pytest

from app.domain.enums import DeclaredRole
from app.extraction.table_parser import carton_count_from_no, parse_pages


@pytest.fixture(autouse=True)
def _isolate_layout_store(tmp_path, monkeypatch):
    from app import config as config_mod
    from app.extraction import layout_memory
    s = config_mod.get_settings_uncached()
    s.storage_dir = tmp_path
    monkeypatch.setattr(layout_memory, "get_settings", lambda: s)


def _parse(pages, locales=None):
    return parse_pages(DeclaredRole.PACKING_LIST, pages, locales or {n: None for n in pages})


# Condition 1 layout: item-wise gross AND net, no quantity column at all —
# the shape `_header_map` used to reject outright (it demanded a qty column).
GN_HEADER = "| C/NO | DESCRIPTION OF GOODS | N.W. (KGS) | G.W. (KGS) |"
GN_ROW_A = "| 1 | SHAMPOO 500 ML | 6.000 | 7.500 |"
GN_ROW_B = "| 2 | HAND WASH 250 ML | 4.000 | 4.500 |"
GN_TOTAL = "| TOTAL | | 10.000 | 12.000 |"


def test_gross_and_net_columns_are_extracted_with_header_units():
    pp = _parse({1: "\n".join([GN_HEADER, GN_ROW_A, GN_ROW_B, GN_TOTAL])}).pages[1]
    assert pp.confirmed and len(pp.rows) == 2
    a = pp.rows[0]
    assert a.description_raw == "SHAMPOO 500 ML"
    assert a.gross_weight.value_raw == "7.500" and a.gross_weight.unit_raw == "KGS"
    assert a.net_weight.value_raw == "6.000" and a.net_weight.unit_raw == "KGS"
    # the unit came from the HEADER cell, not from the value cell
    assert pp.rows[1].gross_weight.value_raw == "4.500"


def test_totals_row_is_never_emitted_as_a_goods_row():
    res = _parse({1: "\n".join([GN_HEADER, GN_ROW_A, GN_ROW_B, GN_TOTAL])})
    assert [r.description_raw for r in res.pages[1].rows] == ["SHAMPOO 500 ML", "HAND WASH 250 ML"]
    # and it is read as what it is: the document's own printed totals
    assert res.printed_totals["gross_wt"][0] == "12.000"
    assert res.printed_totals["net_wt"][0] == "10.000"


def test_parsed_sums_matching_the_printed_total_keep_the_parse():
    res = _parse({1: "\n".join([GN_HEADER, GN_ROW_A, GN_ROW_B, GN_TOTAL])})
    assert res.pages[1].confirmed
    assert any("matches the printed total" in n for n in res.notes)


# The gross column swapped into the net column: every row still parses, the
# arithmetic of any single row still looks fine, and the declaration would be
# silently wrong.  Only the document's own total catches it.
GN_TOTAL_WRONG = "| TOTAL | | 10.000 | 99.000 |"


def test_sum_that_contradicts_the_printed_total_stands_the_parser_down():
    res = _parse({1: "\n".join([GN_HEADER, GN_ROW_A, GN_ROW_B, GN_TOTAL_WRONG])})
    assert res.pages == {}                      # nothing owned -> the LLM path runs
    assert any("does not match the printed total" in n for n in res.notes)


# Condition 2 layout: cartons only.
CT_HEADER = "| SL | DESCRIPTION | QTY | UOM | NO. OF CTNS |"
CT_ROW_A = "| 1 | WIDGET ALPHA | 100 | PCS | 3 |"
CT_ROW_B = "| 2 | WIDGET BETA | 50 | PCS | 2 |"
CT_TOTAL = "| TOTAL | | 150 | | 5 |"


def test_carton_count_column_is_extracted():
    res = _parse({1: "\n".join([CT_HEADER, CT_ROW_A, CT_ROW_B, CT_TOTAL])})
    pp = res.pages[1]
    assert pp.confirmed and [r.carton_count.value_raw for r in pp.rows] == ["3", "2"]
    assert [r.quantity_raw for r in pp.rows] == ["100", "50"]
    assert res.printed_totals["ctn"][0] == "5"


# A carton NUMBER is not a carton COUNT (spec section 4, Condition 2).
CNO_HEADER = "| CARTON NO | DESCRIPTION | G.W. KGS |"
CNO_ROW_A = "| 1-5 | WIDGET ALPHA | 50.000 |"
CNO_ROW_B = "| 6 | WIDGET BETA | 10.000 |"


def test_carton_number_range_becomes_a_count_and_keeps_the_identifier():
    pp = _parse({1: "\n".join([CNO_HEADER, CNO_ROW_A, CNO_ROW_B])}).pages[1]
    assert pp.rows[0].carton_no_raw == "1-5" and pp.rows[0].carton_count.value_raw == "5"
    assert pp.rows[1].carton_no_raw == "6" and pp.rows[1].carton_count.value_raw == "1"


@pytest.mark.parametrize("raw,expected", [
    ("7", 1), ("1-5", 5), ("12-18", 7), ("1,3,5", 3), ("C/NO 12-18", 7),
    ("1 to 4", 4), ("", None), (None, None), ("N/A", None),
])
def test_carton_count_from_number(raw, expected):
    assert carton_count_from_no(raw) == expected


# Rows printed against the SAME carton share it — the group total must be
# divided among them later, never claimed in full by each.
SH_ROW_A = "| 1-2 | WIDGET ALPHA | 10.000 |"
SH_ROW_B = "| 1-2 | WIDGET BETA | 6.000 |"


def test_rows_sharing_a_carton_number_are_marked_as_one_group():
    pp = _parse({1: "\n".join([CNO_HEADER, SH_ROW_A, SH_ROW_B])}).pages[1]
    assert [r.shared_carton_group_raw for r in pp.rows] == ["1-2", "1-2"]


def test_unshared_carton_numbers_are_not_grouped():
    pp = _parse({1: "\n".join([CNO_HEADER, CNO_ROW_A, CNO_ROW_B])}).pages[1]
    assert [r.shared_carton_group_raw for r in pp.rows] == [None, None]


# A column of 1,2,3,… under a count-ish header is a serial, not a count.
SER_HEADER = "| PKGS | DESCRIPTION | G.W. KGS |"
SER_ROWS = ["| 1 | WIDGET A | 10.000 |", "| 2 | WIDGET B | 10.000 |", "| 3 | WIDGET C | 10.000 |"]


def test_consecutive_carton_column_is_treated_as_a_number_not_a_count():
    res = _parse({1: "\n".join([SER_HEADER, *SER_ROWS])})
    rows = res.pages[1].rows
    assert all(r.carton_count is None for r in rows)
    assert [r.carton_no_raw for r in rows] == ["1", "2", "3"]
    assert any("serial numbers" in n for n in res.notes)


# An unlabelled weight column is neither gross nor net and must say so.
ANY_HEADER = "| SL | DESCRIPTION | WEIGHT (KG) |"
ANY_ROW = "| 1 | WIDGET ALPHA | 12.500 |"


def test_unlabelled_weight_column_is_declared_weight_with_unknown_type():
    pp = _parse({1: "\n".join([ANY_HEADER, ANY_ROW])}).pages[1]
    r = pp.rows[0]
    assert r.gross_weight is None and r.net_weight is None
    assert r.declared_weight.value_raw == "12.500" and r.weight_type_raw == "UNKNOWN"


# Batch / expiry / origin columns ride along for free once the map exists.
EXTRA_HEADER = "| SL | DESCRIPTION | BATCH NO | EXPIRY | COUNTRY OF ORIGIN | CTNS | G.W. KGS |"
EXTRA_ROW = "| 1 | WIDGET ALPHA | B-2291 | 2027-04 | DE | 2 | 8.000 |"


def test_batch_expiry_and_origin_columns_are_extracted():
    pp = _parse({1: "\n".join([EXTRA_HEADER, EXTRA_ROW])}).pages[1]
    r = pp.rows[0]
    assert r.batch_no_raw == "B-2291" and r.expiry_date_raw == "2027-04"
    assert r.country_of_origin_raw == "DE" and r.carton_count.value_raw == "2"


def test_description_only_line_is_not_a_row():
    pp = _parse({1: "\n".join([GN_HEADER, "| | CONTINUED FROM PREVIOUS PAGE | | |"])}).pages[1]
    assert pp.rows == []


def test_no_header_anywhere_stands_the_parser_down():
    assert _parse({1: "| 1 | WIDGET | 10.000 | 12.000 |"}).pages == {}


# A page the parser cannot fully own must not be cross-checked against the
# document total: its sums are partial by construction.
def test_partial_ownership_skips_the_totals_cross_check():
    res = _parse({
        1: "\n".join([GN_HEADER, GN_ROW_A, GN_ROW_B]),
        2: "| RONYX22515X 00763000248437 | 3 | EA |",
    })
    assert res.pages[1].confirmed
    assert any("not cross-checked" in n for n in res.notes)
