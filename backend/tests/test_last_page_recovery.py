"""Last-page row-loss defense (2026-07-19 incident, invoice 2025-194-2).

A 3-page invoice lost its final page's 8 goods rows: the header map broke
(unanchored "S.N" matched inside "HSN code"; "Qty (UOM)" stole the qty slot
from "Unit quantity"), rounded printed rates defeated the exact arithmetic
gate, the parser stood down, the LLM window dropped page 3, and the raw-text
qty|UOM completeness regex was blind to the page's bold ``**...**`` cells —
so page_complete=True shipped with zero warnings.  Every layer is covered
here: the parser now owns this vendor deterministically, and the honesty
gate can no longer be fooled by emphasis markers or a lying LLM.
"""
import json

import pytest

from app.domain.enums import DeclaredRole
from app.extraction.common_models import InvoiceChunkRaw, InvoiceLineRaw, RoleValidation
from app.extraction.openai_extractor import OpenAIExtractor, flag_incomplete_pages
from app.extraction.table_parser import page_prints_goods_rows, parse_pages
from app.extraction.validator import _pages_missing_rows
from app.ocr.base import OcrDocument, OcrPage


@pytest.fixture(autouse=True)
def _isolate_layout_store(isolated_vendor_stores):
    """Vendor layout memory starts empty for every test in this file.

    A remembered layout is keyed by role + header signature and is offered
    to any later document whose own header is unreadable — so one test
    recording a layout would feed it into another test's headerless-page
    case, which is exactly the parse those tests assert stands down.
    (Was a tmp storage_dir; the store is a database table now.)
    """


# Condensed replica of the incident document's OCR (markdown tables, bold
# emphasis and rounded rates exactly as Mistral OCR emitted them).
HDR = ("|  Serial number | Description of goods | HSN code | Qty (UOM) | "
       "Unit quantity | Rate Per Pcs | Total Amount  |")
SEP = "| --- | --- | --- | --- | --- | --- | --- |"
P1_ROWS = [
    "|  1 | **Makeup Accessories** | 96159000000 | PCS | 5 | 1.55 | 7.77  |",   # 5x1.55=7.75
    "|  2 | **Masking Tape** | 48114100000 | PCS | 6 | 1.39 | 8.36  |",         # 6x1.39=8.34
    "|  3 | **Reading Glass** | 90041000000 | PCS | 21 | 1.89 | 39.69  |",      # exact
]
P2_ROWS = [
    "|  35 | School and Office Supplies | 39261000000 | PCS | 2 | 0.54 | 1.07  |",
    "|  36 | Shoulder Bag | 42021900000 | PCS | 24 | 2.32 | 55.76  |",          # 24x2.32=55.68
]
P3_ROWS = [  # the lost page: HS codes BOLD, totals banner below
    "|  83 | **Wallet** | **42021900000** | PCS | 3 | 2.65 | 7.96  |",
    "|  84 | **Watch** | **91021900000** | PCS | 39 | 4.68 | 182.41  |",
    "|  90 | **Zipper** | **96071900000** | PCS | 1 | 1.51 | 1.51  |",
]
# The printed totals are the CONDENSED document's own (101 units / 304.53
# goods / +732.77 freight), not the 90-row original's: the parser now
# cross-checks its parsed line values against the totals the invoice prints,
# and a replica whose rows cannot add up to its own total is not a replica of
# anything real.  Every behaviour these tests pin is unchanged.
P3_TAIL = [
    "|  **Total** |   |   |   | **101** |  | **304.53**  |",
    "|  **Freight** |   |   |   |   |   | **732.77**  |",
    "|  **Total Amount Including Freight** |   |   |   |   |   | **1037.30**  |",
    "|  **Amount in Words** | **Two Thousand Ninety Three Dollars** |   |   |   |   |   |",
]
PAGE1 = "\n".join([HDR, SEP, *P1_ROWS])
PAGE2 = "\n".join([HDR, SEP, *P2_ROWS])
PAGE3 = "\n".join([HDR, SEP, *P3_ROWS, *P3_TAIL])


def _parse(pages):
    return parse_pages(DeclaredRole.INVOICE, pages, {n: None for n in pages}, ())


# --------------------------------------------------------------------------- #
# Layer 1 — the parser owns this vendor layout deterministically
# --------------------------------------------------------------------------- #
def test_incident_vendor_header_maps_correctly():
    res = _parse({1: PAGE1})
    m = res.mapping
    assert m["line_no"] == 0            # "Serial number" — not stolen by "HSN code"
    assert m["hs"] == 2
    assert m["qty"] == 4                # "Unit quantity" holds the numbers…
    assert m["uom"] == 3                # …"Qty (UOM)" holds the unit words
    assert m["price"] == 5 and m["total"] == 6


def test_all_pages_owned_including_bold_last_page():
    res = _parse({1: PAGE1, 2: PAGE2, 3: PAGE3})
    assert all(pp.confirmed for pp in res.pages.values())
    assert [len(res.pages[n].rows) for n in (1, 2, 3)] == [3, 2, 3]
    r83 = res.pages[3].rows[0]
    assert r83.line_no_raw == "83" and r83.description_raw == "Wallet"
    assert r83.hs_code_raw == "42021900000"          # bold stripped
    assert r83.quantity_raw == "3" and r83.uom_raw == "PCS"
    assert r83.unit_price_raw == "2.65" and r83.line_total_raw == "7.96"
    assert r83.model_raw is None                     # serial number is NOT a model


def test_hs_code_next_to_uom_never_becomes_the_quantity():
    # "| 42021900000 | PCS |" is an HS column followed by the unit column —
    # the qty|UOM pair scan must not read eleven billion pieces.
    # Parsed as the WHOLE document (page 3 carries the totals row, so parsing it
    # alone would fail the printed-total cross-check by construction).
    res = _parse({1: PAGE1, 2: PAGE2, 3: PAGE3})
    for row in res.pages[3].rows:
        assert float(row.quantity_raw) < 100


def test_rounded_rate_within_tolerance_confirms_but_wrong_math_still_rejected():
    ok = "|  5 | **Gloves** | 61169900000 | PCS | 7 | 1.24 | 8.70  |"    # 8.68 vs 8.70
    bad = "|  6 | **Gloves** | 61169900000 | PCS | 7 | 1.24 | 12.42  |"  # off by 3.74
    res = _parse({1: "\n".join([HDR, SEP, ok, bad])})
    pp = res.pages[1]
    assert len(pp.rows) == 1 and pp.rows[0].line_total_raw == "8.70"
    assert pp.suspicious_leftover == 1 and not pp.confirmed


def test_totals_banner_lines_never_block_page_ownership():
    # page 3 is the one carrying Total / Freight / Amount-in-Words below its rows
    res = _parse({1: PAGE1, 2: PAGE2, 3: PAGE3})
    assert res.pages[3].confirmed and res.pages[3].suspicious_leftover == 0


# --------------------------------------------------------------------------- #
# Layer 2 — the completeness detector sees bold cells
# --------------------------------------------------------------------------- #
def test_page_prints_goods_rows_survives_bold_cells():
    assert page_prints_goods_rows(PAGE3) is True
    assert page_prints_goods_rows("\n".join([HDR, SEP, *P3_TAIL])) is False  # totals only
    assert page_prints_goods_rows("free text, no tables") is False


def test_pages_missing_rows_flags_uncovered_bold_page():
    errs = _pages_missing_rows([], {3: PAGE3})
    assert len(errs) == 1 and "page 3" in errs[0] and errs[0].startswith("PAGE_ROWS_MISSING")
    covered = [InvoiceLineRaw(source_page_no=3, source_row_index=1,
                              description_raw="Wallet", quantity_raw="3")]
    assert _pages_missing_rows(covered, {3: PAGE3}) == []


# --------------------------------------------------------------------------- #
# Layer 4 — the honesty gate: page_complete can never lie again
# --------------------------------------------------------------------------- #
def _payload(rows):
    return InvoiceChunkRaw(
        role_validation=RoleValidation(expected_role=DeclaredRole.INVOICE,
                                       matches_expected_role=True),
        rows=rows, page_complete=True)


def _pages():
    return [OcrPage(page_no=1, plain_text=PAGE1), OcrPage(page_no=3, plain_text=PAGE3)]


def test_flag_incomplete_pages_downgrades_page_complete():
    payload = _payload([InvoiceLineRaw(source_page_no=1, source_row_index=1,
                                       description_raw="Makeup Accessories", quantity_raw="5")])
    payload, warnings = flag_incomplete_pages(DeclaredRole.INVOICE, _pages(), payload, [])
    assert payload.page_complete is False
    assert any("EXTRACTION_INCOMPLETE" in w and "page 3" in w for w in warnings)
    assert any("EXTRACTION_INCOMPLETE" in w for w in payload.warnings)


def test_flag_incomplete_pages_leaves_complete_payloads_alone():
    rows = [InvoiceLineRaw(source_page_no=n, source_row_index=1,
                           description_raw="x", quantity_raw="1") for n in (1, 3)]
    payload, warnings = flag_incomplete_pages(DeclaredRole.INVOICE, _pages(), _payload(rows), [])
    assert payload.page_complete is True and warnings == []


def test_flag_only_applies_to_row_list_roles():
    payload, warnings = flag_incomplete_pages(DeclaredRole.BANKING, _pages(), object(), [])
    assert warnings == []


# --------------------------------------------------------------------------- #
# End-to-end: an LLM that keeps dropping the last page cannot ship silently
# --------------------------------------------------------------------------- #
class _FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def create(self, **kw):
        content = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        msg = type("M", (), {"content": content})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()


class _FakeClient:
    def __init__(self, responses):
        self.chat = type("Chat", (), {"completions": _FakeCompletions(responses)})()


def test_llm_dropping_a_goods_page_is_flagged_not_silent():
    # headerless tables -> parser stands down; the fake LLM returns page-1 rows
    # only, every round, and claims page_complete=True. The deterministic
    # validator forces repair; when rounds are exhausted the honesty gate
    # still flips page_complete and surfaces the loss.
    p1 = "|  Widget one two | 3 | PCS | 1.00 | 3.00  |"
    p2 = "|  Gadget three four | 2 | PCS | 2.00 | 4.00  |"
    ocr = OcrDocument(document_id="d", declared_role=DeclaredRole.INVOICE, pages=[
        OcrPage(page_no=1, plain_text=p1), OcrPage(page_no=2, plain_text=p2)])
    resp = json.dumps({
        "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
        "page_numbers": [1, 2], "page_complete": True,
        "rows": [{"source_page_no": 1, "source_row_index": 1,
                  "description_raw": "Widget one two", "quantity_raw": "3",
                  "uom_raw": "PCS", "unit_price_raw": "1.00", "line_total_raw": "3.00"}],
    })
    ex = OpenAIExtractor(client=_FakeClient([resp]))
    payload, warnings = ex.extract(DeclaredRole.INVOICE, ocr)
    assert [r.source_page_no for r in payload.rows] == [1]
    assert payload.page_complete is False                     # the lie is corrected
    assert any("EXTRACTION_INCOMPLETE" in w and "page 2" in w for w in warnings)
