"""Cross-page continuation fragments must never become declaration items.

2026-07-31 live job (invoice 4050033058: 76 real items, 77 extracted): the last
row of page 16 (CATHETER LA6EBU35, qty 70) spilled its batch/COO breakdown onto
page 17 as a table line keyed only by the parent's GTIN with EMPTY value cells.
Three layers failed in sequence: (1) the fragment counted as a suspicious
leftover and disowned page 17 to the LLM; (2) the LLM emitted the fragment as
its own goods row and INVENTED its value cells ("20 | EA | 24.46 | 122.30" —
printed nowhere on the page); (3) ingest's no-value gate only skips rows whose
price AND total are both absent, so the invented values sailed through as a
phantom 77th item worth 489.20 that the reviewer had to hand-delete.

These tests pin the general fixes at every layer:

* table parser: a value-less fragment (identity token, every value column
  empty) is excused, not a page-disowning leftover — the strictness guard
  (content in ANY unexpected column still disowns) is pinned too;
* neutralize_invented_fragment_values: values an LLM claimed for a row whose
  only matching OCR line provably prints none are removed, loudly;
* reconcile_row_duplicates: value-less rows never count against the printed-
  anchor bound, so an already-neutralized fragment raises no phantom overcount;
* ingest: a value-less fragment row is excluded from the items, named.
"""
import pytest

from app.domain.enums import DeclaredRole
from app.extraction.common_models import (
    Evidence, InvoiceChunkRaw, InvoiceLineRaw, RoleValidation)
from app.extraction.openai_extractor import (
    neutralize_invented_fragment_values,
    reconcile_row_duplicates,
)
from app.extraction.table_parser import parse_pages
from app.reference.store import get_reference
from app.rules.invoice_authority import finalize_invoices


@pytest.fixture(scope="module")
def ref():
    return get_reference()


# The live pages, minimally trimmed.  Page 16 ends with the parent row; its
# batch breakdown continues at the top of page 17 with EMPTY value cells.
HEADER = "|  MODEL NO. | DESCRIPTION | QTY SHIPPED | U/M | UNIT PRICE (USD) | TOTAL (USD)  |"
SEP = "| --- | --- | --- | --- | --- | --- |"
P16_PARENT = "|  LA6EBU35 |  | 70 | EA | 24,46 | 1.712,20  |"
P17_FRAGMENT = ("|  00763000568962 | Batch: 230214140 39 EA COO: Mexico 231755823 4 EA "
                "COO: Mexico 231951351 23 EA COO: Mexico |  |  |  |   |")
P17_ROW_1 = ("|  LA6AL10 00763000565299 | CATHETER LA6AL10 LA 6F 100CM AL10 "
             "Batch: 231951293 5 EA COO: Mexico | 5 | EA | 24.46 | 122.30  |")
P17_ROW_2 = ("|  LA6AL20 00763000565213 | CATHETER LA6AL20 LA 6F 100CM AL20 "
             "Batch: 232153969 5 EA COO: Mexico | 5 | EA | 24.46 | 122.30  |")
PAGE_16 = "\n".join([HEADER, SEP, P16_PARENT])
PAGE_17 = "\n".join([HEADER, SEP, P17_FRAGMENT, P17_ROW_1, P17_ROW_2])


class _P:                          # minimal stand-in for an OcrPage
    def __init__(self, page_no, plain_text):
        self.page_no, self.plain_text = page_no, plain_text


def _payload(rows):
    return InvoiceChunkRaw(
        role_validation=RoleValidation(expected_role=DeclaredRole.INVOICE),
        rows=rows, page_numbers=[16, 17])


def _phantom_row():
    """The LLM's actual invention from the live job, verbatim."""
    return InvoiceLineRaw(
        source_page_no=17, source_row_index=1,
        line_no_raw="00763000568962", model_raw="00763000568962",
        description_raw="Batch: 230214140 39 EA COO: Mexico 231755823 4 EA COO: Mexico",
        quantity_raw="20", uom_raw="EA", unit_price_raw="24.46", line_total_raw="122.30",
        evidence=[Evidence(page_no=17, quote="20 | EA | 24.46 | 122.30")])


def _real_row():
    return InvoiceLineRaw(
        source_page_no=17, source_row_index=2,
        line_no_raw=None, model_raw="LA6AL10",
        description_raw="CATHETER LA6AL10 LA 6F 100CM AL10",
        quantity_raw="5", uom_raw="EA", unit_price_raw="24.46", line_total_raw="122.30",
        evidence=[Evidence(page_no=17, quote=P17_ROW_1)])


# --------------------------------------------------------------------------- #
# layer 1 — table parser: the fragment no longer disowns its page
# --------------------------------------------------------------------------- #
def test_valueless_fragment_is_excused_and_the_page_stays_parser_owned():
    res = parse_pages(DeclaredRole.INVOICE, {17: PAGE_17}, {17: None})
    pp = res.pages[17]
    assert pp.confirmed and pp.suspicious_leftover == 0
    assert [r.model_raw for r in pp.rows] == ["LA6AL10", "LA6AL20"]
    assert any("continuation fragment" in n for n in res.notes)


def test_fragment_with_content_outside_annotation_columns_still_disowns():
    # a value drifted out of its mapped column — the line might be a mangled
    # REAL row, so the page must go to the LLM exactly as before
    drifted = "|  00763000568962 | Batch: 230214140 39 EA |  | 1.712,20 |  |   |"
    page = "\n".join([HEADER, SEP, drifted, P17_ROW_1])
    pp = parse_pages(DeclaredRole.INVOICE, {17: page}, {17: None}).pages[17]
    assert not pp.confirmed and pp.suspicious_leftover == 1


def test_truncated_line_missing_its_value_cells_still_disowns():
    # only 2 cells against a 6-column map: the value cells are MISSING, not
    # provably empty — this can be a split row whose values continue on the
    # next OCR line, so the page must still go to the LLM
    truncated = "|  00763000568962 | Batch: 230214140 39 EA COO: Mexico |"
    page = "\n".join([HEADER, SEP, truncated, P17_ROW_1])
    pp = parse_pages(DeclaredRole.INVOICE, {17: page}, {17: None}).pages[17]
    assert not pp.confirmed and pp.suspicious_leftover == 1


def test_fragment_with_a_qty_uom_pair_still_disowns():
    # a quantity is a value: "| 70 | EA |" can be a real (mangled) goods row
    qty_frag = "|  00763000568962 | Batch: 230214140 | 70 | EA |  |   |"
    page = "\n".join([HEADER, SEP, qty_frag, P17_ROW_1])
    pp = parse_pages(DeclaredRole.INVOICE, {17: page}, {17: None}).pages[17]
    assert not pp.confirmed and pp.suspicious_leftover == 1


# --------------------------------------------------------------------------- #
# layer 2 — LLM path: invented values are provably unprinted and removed
# --------------------------------------------------------------------------- #
def test_invented_fragment_values_are_removed_with_a_loud_warning():
    payload = _payload([_phantom_row(), _real_row()])
    out, warnings = neutralize_invented_fragment_values(
        DeclaredRole.INVOICE, [_P(16, PAGE_16), _P(17, PAGE_17)], payload, [])
    phantom, real = out.rows
    assert phantom.unit_price_raw is None and phantom.line_total_raw is None
    assert real.unit_price_raw == "24.46" and real.line_total_raw == "122.30"
    assert len(out.rows) == 2                       # rows are never removed here
    assert any(w.startswith("FRAGMENT_VALUES_UNPRINTED") for w in warnings)


def test_row_matching_a_valued_line_is_never_touched():
    # the real row's GTIN appears in a line that prints money — hands off, even
    # though its own claimed numbers match that line
    payload = _payload([_real_row()])
    out, warnings = neutralize_invented_fragment_values(
        DeclaredRole.INVOICE, [_P(17, PAGE_17)], payload, [])
    assert out.rows[0].line_total_raw == "122.30" and warnings == []


def test_claimed_number_printed_in_the_fragment_blocks_removal():
    # the row's claimed quantity (39) IS printed in the fragment line — the
    # values are not proven invented, so the row is left for reviewer judgement
    row = _phantom_row()
    row.quantity_raw = "39"
    payload = _payload([row])
    out, warnings = neutralize_invented_fragment_values(
        DeclaredRole.INVOICE, [_P(17, PAGE_17)], payload, [])
    assert out.rows[0].unit_price_raw == "24.46" and warnings == []


def test_row_without_identity_tokens_is_never_touched():
    row = _phantom_row()
    row.line_no_raw = row.model_raw = None
    row.description_raw = "loose text with no code"
    payload = _payload([row])
    out, warnings = neutralize_invented_fragment_values(
        DeclaredRole.INVOICE, [_P(17, PAGE_17)], payload, [])
    assert out.rows[0].unit_price_raw == "24.46" and warnings == []


# --------------------------------------------------------------------------- #
# layer 3 — overcount gate: a neutralized fragment raises no phantom overcount
# --------------------------------------------------------------------------- #
def test_neutralized_fragment_does_not_count_against_the_anchor_bound():
    payload = _payload([_phantom_row(), _real_row()])
    pages = [_P(17, PAGE_17)]
    payload, warnings = neutralize_invented_fragment_values(
        DeclaredRole.INVOICE, pages, payload, [])
    payload, warnings = reconcile_row_duplicates(
        DeclaredRole.INVOICE, pages, payload, warnings)
    assert not any(w.startswith("EXTRACTION_OVERCOUNT") for w in warnings)
    assert any(w.startswith("FRAGMENT_VALUES_UNPRINTED") for w in warnings)


def test_valued_overcount_is_still_flagged():
    # two valued rows against one printed anchor — the honesty gate still fires
    page = "\n".join([HEADER, SEP, P17_ROW_1])
    payload = _payload([_real_row(), _real_row()])
    _, warnings = reconcile_row_duplicates(DeclaredRole.INVOICE, [_P(17, page)], payload, [])
    assert any(w.startswith("EXTRACTION_OVERCOUNT") for w in warnings)


# --------------------------------------------------------------------------- #
# layer 4 — ingest: the value-less fragment row never becomes an item
# --------------------------------------------------------------------------- #
def test_ingest_excludes_the_neutralized_fragment_row(ref):
    phantom = _phantom_row()
    phantom.unit_price_raw = phantom.line_total_raw = None    # post-neutralization
    chunk = InvoiceChunkRaw(
        role_validation=RoleValidation(expected_role=DeclaredRole.INVOICE),
        rows=[phantom, _real_row()], page_numbers=[17])
    inv = finalize_invoices([chunk], ref=ref)
    assert len(inv.items) == 1
    assert inv.items[0].line_total.compare(inv.goods_total) == 0
    assert any(w.code == "ROW_NO_VALUE_SKIPPED" for w in inv.warnings)
