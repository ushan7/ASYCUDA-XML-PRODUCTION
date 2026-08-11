"""The unlabelled weight column — 2026-07-30 live-job root cause.

A real 115-item packing list printed one column headed just "Weight (KG)".
The parser extracted every value correctly into ``declared_weight`` — and the
allocator never read that field, so a document stating every item weight was
allocated by invoice VALUE share.  The extraction was perfect; the evidence
reached nobody.

The classification rule: a row-stated type wins; otherwise the column's own
SUM against the document's printed totals decides (a column summing to the
printed total gross IS the gross breakdown); when nothing settles it, the
column is still the weight SHAPE, flagged.  Same class of gap on the invoice
side: the deterministic parser never emitted ``item_weight_raw``, so an
invoice printing the highest-authority net weight fell to lower rungs.
"""
from decimal import Decimal

import pytest

from app.domain.enums import DeclaredRole
from app.extraction.common_models import PackingListChunkRaw, RoleValidation
from app.extraction.table_parser import parse_pages
from app.rules.models import WorkItem
from app.rules.packing_match import match_packing
from app.rules.weight_carton import allocate_weights_and_cartons

D = Decimal


@pytest.fixture(autouse=True)
def _isolate_layout_store(isolated_vendor_stores):
    """Vendor layout memory starts empty for every test in this file.

    A remembered layout is keyed by role + header signature and is offered
    to any later document whose own header is unreadable — so one test
    recording a layout would feed it into another test's headerless-page
    case, which is exactly the parse those tests assert stands down.
    (Was a tmp storage_dir; the store is a database table now.)
    """


def _item(seq, desc, qty="1", total="100"):
    return WorkItem(
        xml_item_sequence=seq, source_invoice_number="INV-1", source_invoice_date="",
        source_invoice_item_index=seq, source_invoice_item_no=None,
        description_raw=desc, quantity=D(qty), invoice_uom_raw="PCS",
        unit_price=D("1"), line_total=D(total), currency="USD")


def _payload(rows, **header):
    return PackingListChunkRaw.model_validate({
        "role_validation": {"expected_role": "PACKING_LIST", "matches_expected_role": True},
        "rows": [{"source_page_no": 1, "source_row_index": i + 1, **r} for i, r in enumerate(rows)],
        **header,
    })


def _declared(desc, kg, wtype=None):
    row = {"description_raw": desc,
           "declared_weight": {"value_raw": kg, "unit_raw": "KG"}}
    if wtype:
        row["weight_type_raw"] = wtype
    return row


# --------------------------------------------------------------------------- #
# classification by the document's own arithmetic
# --------------------------------------------------------------------------- #
def test_column_summing_to_the_printed_gross_total_is_the_gross_breakdown():
    items = [_item(1, "ADAPTAR", total="900"), _item(2, "ALARM CLOCK", total="100")]
    warns = []
    ev = match_packing(items, [_payload(
        [_declared("ADAPTAR", "0.85"), _declared("ALARM CLOCK", "0.77")],
        total_gross_weight={"value_raw": "1.62", "unit_raw": "KG"})], warnings_out=warns)
    assert ev[1].gross_weight == D("0.85") and ev[2].gross_weight == D("0.77")
    assert any(w.code == "PACKING_WEIGHT_TYPE_INFERRED" and "GROSS" in w.message for w in warns)


def test_column_summing_to_the_printed_net_total_is_the_net_breakdown():
    items = [_item(1, "ADAPTAR"), _item(2, "ALARM CLOCK")]
    warns = []
    ev = match_packing(items, [_payload(
        [_declared("ADAPTAR", "0.85"), _declared("ALARM CLOCK", "0.77")],
        total_gross_weight={"value_raw": "2.00", "unit_raw": "KG"},
        total_net_weight={"value_raw": "1.62", "unit_raw": "KG"})], warnings_out=warns)
    assert ev[1].net_weight == D("0.85") and ev[1].gross_weight is None
    assert any("NET" in w.message for w in warns if w.code == "PACKING_WEIGHT_TYPE_INFERRED")


def test_a_row_stated_type_outranks_the_arithmetic():
    items = [_item(1, "ADAPTAR")]
    ev = match_packing(items, [_payload(
        [_declared("ADAPTAR", "0.85", wtype="NET")],
        total_gross_weight={"value_raw": "0.85", "unit_raw": "KG"})])
    assert ev[1].net_weight == D("0.85") and ev[1].gross_weight is None


def test_a_column_matching_no_total_is_still_the_shape_and_says_so():
    items = [_item(1, "ADAPTAR"), _item(2, "ALARM CLOCK")]
    warns = []
    ev = match_packing(items, [_payload(
        [_declared("ADAPTAR", "0.85"), _declared("ALARM CLOCK", "0.77")],
        total_gross_weight={"value_raw": "99", "unit_raw": "KG"})], warnings_out=warns)
    assert ev[1].gross_weight == D("0.85")            # shape: rescaled downstream
    assert any("SHAPE" in w.message for w in warns if w.code == "PACKING_WEIGHT_TYPE_INFERRED")


def test_labelled_columns_are_untouched_by_classification():
    items = [_item(1, "ADAPTAR")]
    ev = match_packing(items, [_payload([
        {"description_raw": "ADAPTAR",
         "gross_weight": {"value_raw": "1.0", "unit_raw": "KG"},
         "declared_weight": {"value_raw": "9.9", "unit_raw": "KG"}},
    ])])
    assert ev[1].gross_weight == D("1.0")             # the labelled value wins


# --------------------------------------------------------------------------- #
# the live job, in miniature: parse -> match -> allocate == the document
# --------------------------------------------------------------------------- #
LIVE_PAGE = "\n".join([
    "| SN | Product Description | HSN Code | Qty | Weight (KG) |",
    "| 1 | Adaptar | 85044090900 | 3 | 0.85 |",
    "| 2 | Alarm Clock | 91051100000 | 10 | 0.77 |",
    "| 3 | Antenna | 74082900000 | 1 | 0.09 |",
    "| TOTAL | | | 14 | 1.71 |",
])


def test_live_job_regression_unlabelled_weight_column_allocates_exactly():
    res = parse_pages(DeclaredRole.PACKING_LIST, {1: LIVE_PAGE}, {1: None})
    assert res.pages[1].confirmed
    rows = res.pages[1].rows
    assert all(r.declared_weight is not None and r.weight_type_raw == "UNKNOWN" for r in rows)
    payload = PackingListChunkRaw(
        role_validation=RoleValidation(expected_role=DeclaredRole.PACKING_LIST),
        rows=rows, total_gross_weight={"value_raw": "1.71", "unit_raw": "KG"})
    items = [_item(1, "Adaptar", qty="3", total="900"),
             _item(2, "Alarm Clock", qty="10", total="50"),
             _item(3, "Antenna", qty="1", total="50")]
    ev = match_packing(items, [payload])
    msgs = allocate_weights_and_cartons(items, ev, D("1.71"), D("3"))
    # EXACTLY the document's figures — not a value-share split
    assert [i.gross_weight_kg for i in items] == [D("0.850"), D("0.770"), D("0.090")]
    assert items[0].allocation_audit["gross_weight_source"] == "packing-list gross weight"
    assert not any(m.code == "WEIGHT_BASIS_VALUE" for m in msgs)


# --------------------------------------------------------------------------- #
# the invoice-side twin: a printed weight column reaches item_weight_raw
# --------------------------------------------------------------------------- #
def test_invoice_weight_column_is_parsed_into_item_weight():
    res = parse_pages(DeclaredRole.INVOICE, {1: "\n".join([
        "| SN | Description | Qty | UOM | Net Wt. (KG) | Rate | Amount |",
        "| 1 | WIDGET ALPHA | 4 | PCS | 2.50 | 25.00 | 100.00 |",
    ])}, {1: None})
    r = res.pages[1].rows[0]
    assert r.item_weight_raw == "2.50" and r.item_weight_unit_raw == "KG"


def test_invoice_gross_weight_column_is_never_the_item_weight():
    """An invoice's GROSS column includes packaging — it is not the net."""
    res = parse_pages(DeclaredRole.INVOICE, {1: "\n".join([
        "| SN | Description | Qty | UOM | Gross Wt. (KG) | Rate | Amount |",
        "| 1 | WIDGET ALPHA | 4 | PCS | 3.10 | 25.00 | 100.00 |",
    ])}, {1: None})
    assert res.pages[1].rows[0].item_weight_raw is None


# --------------------------------------------------------------------------- #
# GENERALITY — the rules must hold for spellings and layouts never seen yet
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("header", [
    "Weight (KG)", "WEIGHT", "WT (KGS)", "Wt. in Kgs", "MASS KG",
    "Total Weight (KG)", "TOTAL WT (KG)", "Item Weight (KG)",
])
def test_any_row_total_weight_header_spelling_is_captured(header):
    res = parse_pages(DeclaredRole.PACKING_LIST, {1: "\n".join([
        f"| SN | Product Description | Qty | {header} |",
        "| 1 | WIDGET ALPHA | 3 | 0.85 |",
    ])}, {1: None})
    r = res.pages[1].rows[0]
    assert r.declared_weight is not None and r.declared_weight.value_raw == "0.85"


@pytest.mark.parametrize("header", ["Unit Weight (KG)", "WEIGHT PER PC (KG)", "WT/PC (KG)"])
def test_per_unit_weight_headers_are_never_a_row_total(header):
    """A per-unit rate declared as the row's weight is wrong for every vendor."""
    res = parse_pages(DeclaredRole.PACKING_LIST, {1: "\n".join([
        f"| SN | Product Description | Qty | {header} | CTNS |",
        "| 1 | WIDGET ALPHA | 3 | 0.85 | 2 |",
    ])}, {1: None})
    r = res.pages[1].rows[0]
    assert r.declared_weight is None and r.gross_weight is None and r.net_weight is None


def test_total_weight_header_is_not_the_money_column():
    """On an invoice, 'Total Weight' must not claim the money-total mapping —
    the arithmetic gate would silently disown every row on the page."""
    res = parse_pages(DeclaredRole.INVOICE, {1: "\n".join([
        "| SN | Description | Qty | UOM | Total Weight (KG) | Rate | Total Amount |",
        "| 1 | WIDGET ALPHA | 4 | PCS | 3.10 | 25.00 | 100.00 |",
    ])}, {1: None})
    r = res.pages[1].rows[0]
    assert r.line_total_raw == "100.00" and r.item_weight_raw == "3.10"


def test_declared_column_in_pounds_converts_before_the_totals_comparison():
    """The classification arithmetic is unit-aware for any future unit, not
    hardwired to kilograms."""
    items = [_item(1, "WIDGET ALPHA"), _item(2, "WIDGET BETA")]
    warns = []
    ev = match_packing(items, [_payload(
        [{"description_raw": "WIDGET ALPHA", "declared_weight": {"value_raw": "10", "unit_raw": "LBS"}},
         {"description_raw": "WIDGET BETA", "declared_weight": {"value_raw": "10", "unit_raw": "LBS"}}],
        total_gross_weight={"value_raw": "9.07", "unit_raw": "KG"})], warnings_out=warns)
    assert ev[1].gross_weight == D("10") * D("0.45359237")
    assert any("GROSS" in w.message for w in warns if w.code == "PACKING_WEIGHT_TYPE_INFERRED")


def test_a_partially_labelled_column_has_one_meaning():
    """One column, one meaning: when the only labelled rows say NET, the bare
    rows of the same column inherit it — a column that mixed net and gross
    values row by row would be two columns, and the schema captures those as
    the labelled gross/net fields instead."""
    items = [_item(1, "WIDGET ALPHA"), _item(2, "WIDGET BETA")]
    ev = match_packing(items, [_payload(
        [_declared("WIDGET ALPHA", "1.00", wtype="NET"),
         _declared("WIDGET BETA", "2.00")],
        total_gross_weight={"value_raw": "3.00", "unit_raw": "KG"})])
    assert ev[1].net_weight == D("1.00") and ev[1].gross_weight is None
    assert ev[2].net_weight == D("2.00") and ev[2].gross_weight is None


def test_contradictory_labels_fall_to_arithmetic_but_each_row_keeps_its_own():
    """GROSS and NET labels in one column contradict each other, so the bare
    rows are decided by the sum — while every labelled row keeps its own
    statement, whatever the column-level arithmetic concludes."""
    items = [_item(1, "WIDGET ALPHA"), _item(2, "WIDGET BETA"), _item(3, "WIDGET GAMMA")]
    ev = match_packing(items, [_payload(
        [_declared("WIDGET ALPHA", "1.00", wtype="NET"),
         _declared("WIDGET BETA", "2.00", wtype="GROSS"),
         _declared("WIDGET GAMMA", "3.00")],
        total_gross_weight={"value_raw": "6.00", "unit_raw": "KG"})])
    assert ev[1].net_weight == D("1.00") and ev[1].gross_weight is None
    assert ev[2].gross_weight == D("2.00") and ev[2].net_weight is None
    assert ev[3].gross_weight == D("3.00")        # sum 6.00 == printed gross total


def test_two_documents_classify_independently():
    """One supplier's column may be gross and another's net — per payload."""
    items = [_item(1, "WIDGET ALPHA"), _item(2, "WIDGET BETA")]
    doc_gross = _payload([_declared("WIDGET ALPHA", "5.00")],
                         total_gross_weight={"value_raw": "5.00", "unit_raw": "KG"})
    doc_net = _payload([_declared("WIDGET BETA", "3.00")],
                       total_gross_weight={"value_raw": "4.00", "unit_raw": "KG"},
                       total_net_weight={"value_raw": "3.00", "unit_raw": "KG"})
    ev = match_packing(items, [doc_gross, doc_net])
    assert ev[1].gross_weight == D("5.00")
    assert ev[2].net_weight == D("3.00") and ev[2].gross_weight is None
