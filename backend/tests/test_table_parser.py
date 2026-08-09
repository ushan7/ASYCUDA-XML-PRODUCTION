"""Step-2 deterministic table parser: code owns the clean pages, the LLM only
sees the residue, and merged output preserves document order."""
import json

import pytest

from app.domain.enums import DeclaredRole
from app.extraction.openai_extractor import OpenAIExtractor
from app.extraction.table_parser import PARSER_EVIDENCE_LABEL, parse_pages
from app.ocr.base import OcrDocument, OcrPage


@pytest.fixture(autouse=True)
def _isolate_layout_store(tmp_path, monkeypatch):
    """Point vendor layout memory at an empty tmp dir so these tests never read
    the real storage/vendor_layouts.json (a populated store would non-
    deterministically feed remembered layouts into the headerless-page cases)."""
    from app import config as config_mod
    from app.extraction import layout_memory
    s = config_mod.get_settings_uncached()
    s.storage_dir = tmp_path
    monkeypatch.setattr(layout_memory, "get_settings", lambda: s)

HEADER = "|  MODEL NO. | DESCRIPTION | QTY SHIPPED | U/M | UNIT PRICE (USD) | TOTAL (USD)  |"
ROW_A = "|  RONYX22515X 00763000248437 | STENT RONYX22515X ONYX 2.25X15RX | 3 | EA | 320.00 | 960.00  |"
ROW_B = "|  RONYX22522X 00763000248451 | STENT RONYX22522X ONYX 2.25X22RX | 2 | EA | 320.00 | 640.00  |"
ROW_EU_MERGED = "|  NCEUP3008X | BALLOON NCEUP3008X NCEUPHORA RX MDR | 20 | EA | 35,00 700,00  |"
ROW_BAD_MATH = "|  RONYX9999X 00763000249999 | STENT RONYX9999X | 3 | EA | 320.00 | 999.00  |"
FRAGMENT = "|  00763000726577 | Batch: 232529045 20 EA COO: Mexico |  |  |   |"
BANNER = "|  CUSTOMER P.O. NO. | SSCPL-MIL-011-VAS-2025-26  |"
# The figure matches ROW_A, the only goods row these fixtures put beside it.
# It used to be an arbitrary 89,975.52, which mattered once the parser began
# cross-checking its parsed line values against the total the page prints: a
# page claiming a hundred times what its rows add up to is one the gate should
# reject, so the fixture was contradicting the thing it is not testing.
TOTAL_LINE = "|  TOTAL (USD) | 960.00  |"


def _parse(pages, locales=None, fallbacks=()):
    return parse_pages(DeclaredRole.INVOICE, pages,
                       locales or {n: None for n in pages}, fallbacks).pages


def test_arithmetic_verified_rows_confirm_the_page():
    pp = _parse({1: "\n".join([HEADER, ROW_A, ROW_B, BANNER])})[1]
    assert pp.confirmed and len(pp.rows) == 2
    r = pp.rows[0]
    assert r.quantity_raw == "3" and r.unit_price_raw == "320.00" and r.line_total_raw == "960.00"
    assert r.model_raw.startswith("RONYX22515X")
    assert r.evidence[0].label == PARSER_EVIDENCE_LABEL
    assert "RONYX22515X" in r.evidence[0].quote


def test_bad_arithmetic_row_disowns_the_page():
    pp = _parse({1: "\n".join([HEADER, ROW_A, ROW_BAD_MATH])})[1]
    assert not pp.confirmed
    assert len(pp.rows) == 1 and pp.suspicious_leftover == 1


# Amount-only invoice: a printed line total but no usable per-unit rate — the
# arithmetic gate can never fire, so these used to fall entirely to the LLM.
AMT_HEADER = "|  ITEM CODE | DESCRIPTION | QTY | UOM | RATE | TOTAL AMT IN INR  |"
AMT_ROW_NO_RATE = "|  SKU-100 | WIDGET ALPHA | 4 | PCS |  | 12,340.00  |"
AMT_ROW_ZERO_RATE = "|  SKU-200 | WIDGET BETA | 2 | PCS | 0.00 | 75,000.00  |"
AMT_ROW_ZERO_TOTAL = "|  SKU-300 | WIDGET GAMMA | 1 | PCS | 0.00 | 0.00  |"


def test_amount_only_rows_confirmed_from_printed_total():
    pp = _parse({1: "\n".join([AMT_HEADER, AMT_ROW_NO_RATE, AMT_ROW_ZERO_RATE])})[1]
    assert pp.confirmed and len(pp.rows) == 2
    assert pp.rows[0].line_total_raw == "12,340.00" and pp.rows[0].unit_price_raw is None
    assert pp.rows[1].line_total_raw == "75,000.00" and pp.rows[1].unit_price_raw is None


def test_zero_total_amount_only_row_not_confirmed():
    # a 0.00 total with no rate is ambiguous (blank column / FOC) -> leave to LLM,
    # never confirm a 0-priced goods row from the fast path
    pp = _parse({1: "\n".join([AMT_HEADER, AMT_ROW_NO_RATE, AMT_ROW_ZERO_TOTAL])})[1]
    assert len(pp.rows) == 1 and not pp.confirmed and pp.suspicious_leftover == 1


# Real-world Indian invoice: quantity merged with unit and NO space ("6PCS",
# "6PC"), and on 0-rate rows the "Amount" column is blank so the real value
# sits one column over. Regression for item prices resolving to 0.
INR_HDR = "|  Sn | Description of Goods | Corrugated Box No. | HSN | Qty./ Unit | Rate In INR | Amount In INR | Taxable AMT.IN INR | IGST % | IGST AMT. INR  |"
INR_ROW1 = "|  1 | DEBAKEY FORCEPS 6\" | 15CM 1MM | 9018 | 6PCS | 325.0000 | 1950.00 | 2250.00 |  | 2250.00  |"
INR_ROW3 = "|  3 | DEBAKEY FORCEPS | 20CM 2MM | 9018 | 6PC | 0.0000 |  | 2700.00 | 5.00 | 2700.00  |"


def test_nospace_merged_qty_and_shifted_amount_recovered():
    pp = _parse({1: "\n".join([INR_HDR, INR_ROW1, INR_ROW3])})[1]
    assert pp.confirmed and len(pp.rows) == 2
    # "6PCS" recognized as qty 6 / PCS despite no space; arithmetic-confirmed on
    # the amount (6*325=1950) but the reported value is the Taxable column (2250)
    assert pp.rows[0].quantity_raw == "6" and pp.rows[0].uom_raw == "PCS"
    assert pp.rows[0].unit_price_raw == "325.0000" and pp.rows[0].line_total_raw == "2250.00"
    # 0-rate row: blank Amount column, value recovered from the next money cell
    assert pp.rows[1].quantity_raw == "6" and pp.rows[1].uom_raw == "PC"
    assert pp.rows[1].line_total_raw == "2700.00"


def test_taxable_value_preferred_over_amount_column():
    # when both a qty*rate "Amount" and a separate "Taxable/Assessable" value are
    # printed, the assessable value (the figure the invoice total sums) wins —
    # so the per-item sum reconciles to the invoice's printed total
    hdr = "|  Sn | Description | HSN | Qty | Rate | Amount | Assessable Value  |"
    r = "|  1 | WIDGET | 9018 | 6PCS | 325.00 | 1950.00 | 2250.00  |"   # amount != assessable
    pp = _parse({1: "\n".join([hdr, r])})[1]
    assert pp.confirmed and len(pp.rows) == 1
    assert pp.rows[0].line_total_raw == "2250.00"       # assessable, not 1950
    assert pp.rows[0].unit_price_raw == "325.00"        # rate still recorded


def test_single_value_column_unchanged():
    # a simple invoice with only one value column still uses it (no regression)
    hdr = "|  MODEL NO. | DESCRIPTION | QTY SHIPPED | U/M | UNIT PRICE (USD) | TOTAL (USD)  |"
    pp = _parse({1: "\n".join([hdr, ROW_A])})[1]
    assert pp.confirmed and pp.rows[0].line_total_raw == "960.00"      # not 0, not None


def test_unitless_quantity_row_confirmed_from_total():
    # Option A: a bare quantity with no unit is still a goods row when a printed
    # total backs it. Mirrors the dropped "item 18".
    #
    # The UOM stays NULL rather than defaulting to "PCS" (2026-08-04): defaulting
    # made an unreadable unit indistinguishable from a printed one, so a shifted
    # column map shipped 15 rows of "PCS" against an invoice printing KGM/PRS/MTR
    # and nothing downstream could tell.  An absent unit is now an empty field the
    # reviewer fills in (ITEM_UOM_MISSING) — a question, not a silent assertion.
    unitless = "|  SKU-400 | WIDGET DELTA | 3 |  |  | 900.00  |"
    pp = _parse({1: "\n".join([AMT_HEADER, AMT_ROW_NO_RATE, unitless])})[1]
    assert pp.confirmed and len(pp.rows) == 2
    assert pp.rows[1].quantity_raw == "3" and pp.rows[1].uom_raw is None
    assert pp.rows[1].line_total_raw == "900.00"        # not dropped, not 0


def test_box_no_header_not_mapped_as_quantity():
    # "Box No." must not claim the qty column (the \bNOS?\b false-match)
    from app.extraction.table_parser import _cells, _header_map
    m = _header_map(_cells("| Sn | Description | Corrugated Box No. | HSN | Qty./ Unit | Rate | Amount |"))
    assert m["qty"] == 4                                 # Qty./ Unit, not Box No. (col 2)


def test_size_annotation_not_misread_as_quantity():
    # a bare length cell must NOT be picked up as a no-space merged quantity
    from app.extraction.manifest import qty_uom_cell_at
    assert qty_uom_cell_at(["Widget", "20CM", "9018", "6PCS", "10.00", "60.00"])[2:] == ("6", "PCS")


def test_merged_money_cell_recovered_when_arithmetic_proves_it():
    pp = _parse({1: "\n".join([HEADER, ROW_EU_MERGED])}, {1: "EU"})[1]
    assert pp.confirmed and pp.rows[0].unit_price_raw == "35,00"
    assert pp.rows[0].line_total_raw == "700,00"


def test_fragments_and_banners_never_block_but_fragments_disown():
    # this FRAGMENT is TRUNCATED (5 cells against a 6-column map — its total
    # column is missing, not provably empty), so it could be a split row and
    # must still disown the page.  A fragment whose value cells are all present
    # and empty is excused instead — pinned in test_continuation_fragments.py.
    pp = _parse({1: "\n".join([HEADER, ROW_A, FRAGMENT])})[1]
    assert len(pp.rows) == 1 and pp.suspicious_leftover == 1 and not pp.confirmed
    pp2 = _parse({1: "\n".join([HEADER, ROW_A, BANNER, TOTAL_LINE])})[1]
    assert pp2.confirmed


def test_no_header_mapping_anywhere_stands_down():
    assert _parse({1: ROW_A + "\n" + ROW_B}) == {}


# --------------------------------------------------------------------------- #
# integration: parser-owned pages skip the LLM entirely
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


GARBLED_PAGE2 = "|  NCEUP2512X 00763000726461 | COO: Mexico 232369814 8 EA COO: Mexico BALLOON split row |"

HDR_RESP = json.dumps({
    "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
    "page_numbers": [1, 3],
    "header": {"invoice_number_raw": "4050033058"},
    "totals": {"grand_total_raw": "2,880.00"},
    "rows": [],
})
RESIDUE_RESP = json.dumps({
    "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
    "page_numbers": [2],
    "rows": [{"source_page_no": 2, "source_row_index": 1,
              "description_raw": "BALLOON NCEUP2512X NCEUPHORA RX MDR",
              "quantity_raw": "8", "uom_raw": "EA", "unit_price_raw": "160.00",
              "line_total_raw": "1,280.00"}],
})


class _RoutedCompletions:
    """Content-routed fake: returns the residue response when the request
    carries page-2 OCR, else the header/totals response. Order-independent, so
    it stays correct when the header and residue calls overlap under the
    process-wide LLM gate (call-order fakes flake there)."""
    def __init__(self, residue_marker, residue_resp, default_resp):
        self._marker, self._residue, self._default = residue_marker, residue_resp, default_resp
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        blob = json.dumps(kw.get("messages", []))
        content = self._residue if self._marker in blob else self._default
        msg = type("M", (), {"content": content})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()


class _RoutedClient:
    def __init__(self, residue_marker, residue_resp, default_resp):
        self.chat = type("Chat", (), {
            "completions": _RoutedCompletions(residue_marker, residue_resp, default_resp)})()


def test_parser_first_extraction_calls_llm_only_for_header_and_residue():
    ocr = OcrDocument(document_id="d", declared_role=DeclaredRole.INVOICE, pages=[
        OcrPage(page_no=1, plain_text="\n".join([HEADER, ROW_A, ROW_B])),
        OcrPage(page_no=2, plain_text=GARBLED_PAGE2),
        OcrPage(page_no=3, plain_text=TOTAL_LINE),
    ])
    # route by the residue-window instruction (absent from the header call),
    # NOT by OCR content — a short page's text also survives the header call
    ex = OpenAIExtractor(client=_RoutedClient(
        "Extract EVERY goods row that STARTS", RESIDUE_RESP, HDR_RESP))
    payload, warnings = ex.extract(DeclaredRole.INVOICE, ocr)
    assert ex._client.chat.completions.calls == 2           # header + one residue window
    assert len(payload.rows) == 3
    assert [r.source_page_no for r in payload.rows] == [1, 1, 2]
    assert payload.rows[0].evidence[0].label == PARSER_EVIDENCE_LABEL   # parser rows
    assert payload.rows[2].description_raw.startswith("BALLOON")        # LLM row
    assert payload.header.invoice_number_raw == "4050033058"
    assert payload.totals.grand_total_raw == "2,880.00"
    assert any("TABLE_PARSER" in w for w in warnings)


def test_headerless_doc_parses_via_remembered_layout():
    """Vendor layout memory: no readable header anywhere, but a remembered
    mapping + arithmetic verification recovers the rows (>= 3 required)."""
    ROW_C = "|  RONYX22526X 00763000248468 | STENT RONYX22526X ONYX 2.25X26RX | 2 | EA | 320.00 | 640.00  |"
    pages = {1: "\n".join([ROW_A, ROW_B, ROW_C])}          # NO header line
    fb = [{"qty": 2, "uom": 3, "price": 4, "total": 5, "desc": 1, "model": 0, "n_cols": 6}]
    res = parse_pages(DeclaredRole.INVOICE, pages, {1: None}, fallback_mappings=fb)
    assert res.from_memory and res.pages[1].confirmed
    assert len(res.pages[1].rows) == 3


def test_headerless_doc_below_memory_threshold_stands_down():
    pages = {1: ROW_A}                                      # only 1 verifiable row
    fb = [{"qty": 2, "uom": 3, "price": 4, "total": 5, "desc": 1, "n_cols": 6}]
    res = parse_pages(DeclaredRole.INVOICE, pages, {1: None}, fallback_mappings=fb)
    assert res.pages == {}


def test_layout_memory_roundtrip(tmp_path, monkeypatch):
    from app import config as config_mod
    from app.extraction import layout_memory
    settings = config_mod.get_settings_uncached()
    settings.storage_dir = tmp_path
    monkeypatch.setattr(layout_memory, "get_settings", lambda: settings)

    mapping = {"qty": 2, "uom": 3, "price": 4, "total": 5, "desc": 1, "n_cols": 6}
    layout_memory.record_layout(DeclaredRole.INVOICE, mapping, "MODELNO|DESCRIPTION|QTY",
                                "Medtronic International Ltd", 57)
    layout_memory.record_layout(DeclaredRole.INVOICE, mapping, "MODELNO|DESCRIPTION|QTY",
                                None, 40)                   # second doc, lower count
    got = layout_memory.stored_layouts(DeclaredRole.INVOICE)
    assert got == [mapping]
    data = json.loads((tmp_path / "vendor_layouts.json").read_text(encoding="utf-8"))
    assert data["layouts"][0]["docs"] == 2
    assert data["layouts"][0]["confirmed_rows"] == 57       # max, not last
    assert data["layouts"][0]["vendor_hint"] == "Medtronic International Ltd"
    assert layout_memory.stored_layouts(DeclaredRole.PACKING_LIST) == []


def test_layout_memory_rejects_thin_evidence(tmp_path, monkeypatch):
    from app import config as config_mod
    from app.extraction import layout_memory
    settings = config_mod.get_settings_uncached()
    settings.storage_dir = tmp_path
    monkeypatch.setattr(layout_memory, "get_settings", lambda: settings)
    layout_memory.record_layout(DeclaredRole.INVOICE, {"qty": 2}, "SIG", None, 2)  # < 3 rows
    assert layout_memory.stored_layouts(DeclaredRole.INVOICE) == []


def test_parser_stands_down_without_header_and_uses_llm_path():
    ocr = OcrDocument(document_id="d", declared_role=DeclaredRole.INVOICE, pages=[
        OcrPage(page_no=1, plain_text=ROW_A),
    ])
    full = json.dumps({
        "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
        "page_numbers": [1],
        "rows": [{"source_page_no": 1, "source_row_index": 1,
                  "description_raw": "STENT RONYX22515X ONYX 2.25X15RX",
                  "quantity_raw": "3", "uom_raw": "EA",
                  "unit_price_raw": "320.00", "line_total_raw": "960.00"}],
    })
    ex = OpenAIExtractor(client=_FakeClient([full]))
    payload, _ = ex.extract(DeclaredRole.INVOICE, ocr)
    assert ex._client.chat.completions.calls == 1            # historical single-shot path
    assert len(payload.rows) == 1 and not payload.rows[0].evidence
