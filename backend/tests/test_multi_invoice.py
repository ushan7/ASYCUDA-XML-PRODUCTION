"""Multi-invoice attachments: one uploaded PDF bundling several printed
invoices (e.g. "INV ALL7_merged.pdf" = 7 invoices).

Pins the three guarantees:
  1. sub_invoices survive window extraction and merge (partial entries from
     different windows unify by invoice number, field-wise);
  2. goods-row ORDER is the flat printed order — page ascending, within-page
     emission order — and can never be scrambled by grouping or by bogus
     LLM-provided row indices;
  3. the authority layer splits a chunk into per-invoice groups (contiguous
     slices of the flat list), checks each invoice's own printed totals, and
     numbers items in document order.
"""
import json

from decimal import Decimal

from app.domain.enums import DeclaredRole
from app.extraction.common_models import InvoiceChunkRaw, SubInvoiceRaw
from app.extraction.openai_extractor import (
    OpenAIExtractor,
    _header_zone_pages,
    _merge_chunk_payloads,
    _merge_sub_invoices,
)
from app.ocr.base import OcrDocument, OcrPage
from app.rules.invoice_authority import finalize_invoices

ROLE_OK = {"expected_role": "INVOICE", "matches_expected_role": True}


def _row(page, idx, desc, total="100.00", **kw):
    return {"source_page_no": page, "source_row_index": idx, "description_raw": desc,
            "quantity_raw": "1", "uom_raw": "EA", "unit_price_raw": total,
            "line_total_raw": total, **kw}


def _chunk(rows, subs=None, header=None, totals=None):
    return InvoiceChunkRaw.model_validate({
        "role_validation": ROLE_OK, "rows": rows,
        "sub_invoices": subs or [], "header": header, "totals": totals,
    })


# --------------------------------------------------------------------------- #
# _merge_sub_invoices — partial per-window entries unify
# --------------------------------------------------------------------------- #
def test_sub_invoice_entries_merge_by_number_field_wise():
    header_part = SubInvoiceRaw.model_validate(
        {"invoice_number_raw": "INV-2", "invoice_date_raw": "05.04.2026", "first_page_no": 5})
    totals_part = SubInvoiceRaw.model_validate(
        {"invoice_number_raw": "INV- 2",              # OCR spacing variant
         "totals": {"grand_total_raw": "1,234.00"}})
    other = SubInvoiceRaw.model_validate({"invoice_number_raw": "INV-1", "first_page_no": 1})
    unanchored = SubInvoiceRaw.model_validate({"invoice_date_raw": "05.04.2026"})  # no number, no page

    merged = _merge_sub_invoices([header_part, totals_part, other, unanchored])
    assert [s.invoice_number_raw for s in merged] == ["INV-1", "INV-2"]   # ordered by start page
    inv2 = merged[1]
    assert inv2.first_page_no == 5
    assert inv2.invoice_date_raw == "05.04.2026"
    assert inv2.totals.grand_total_raw == "1,234.00"   # totals window's contribution


def test_sub_invoice_merge_keeps_earliest_start_page():
    a = SubInvoiceRaw.model_validate({"invoice_number_raw": "X", "first_page_no": 7})
    b = SubInvoiceRaw.model_validate({"invoice_number_raw": "X", "first_page_no": 4})
    merged = _merge_sub_invoices([a, b])
    assert len(merged) == 1 and merged[0].first_page_no == 4


# --------------------------------------------------------------------------- #
# _merge_chunk_payloads — document order is page order + emission order,
# immune to bogus LLM row indices
# --------------------------------------------------------------------------- #
def test_window_merge_orders_rows_by_page_keeping_emission_order():
    # window 2 arrives with all row indices wrongly set to 1 — a sort keyed on
    # (page, row_index) would scramble the printed order; the stable page sort
    # must keep each page's emission order intact
    w1 = _chunk([_row(1, 1, "A1"), _row(2, 1, "B1"), _row(2, 2, "B2")],
                subs=[{"invoice_number_raw": "INV-A", "first_page_no": 1}])
    w2 = _chunk([_row(3, 1, "C1"), _row(3, 1, "C2"), _row(3, 1, "C3")],
                subs=[{"invoice_number_raw": "INV-B", "first_page_no": 3,
                       "totals": {"grand_total_raw": "300.00"}}])
    merged = _merge_chunk_payloads(DeclaredRole.INVOICE, [w1, w2])
    assert [r.description_raw for r in merged.rows] == ["A1", "B1", "B2", "C1", "C2", "C3"]
    assert [s.invoice_number_raw for s in merged.sub_invoices] == ["INV-A", "INV-B"]


def test_window_merge_reorders_out_of_order_window_payloads():
    # defensive: even if window payloads arrive out of page order (parallel
    # window extraction later), rows end up in document order
    w_late = _chunk([_row(3, 1, "C1")])
    w_early = _chunk([_row(1, 1, "A1"), _row(2, 1, "B1")])
    merged = _merge_chunk_payloads(DeclaredRole.INVOICE, [w_late, w_early])
    assert [r.description_raw for r in merged.rows] == ["A1", "B1", "C1"]


# --------------------------------------------------------------------------- #
# finalize_invoices — per-invoice split without touching order
# --------------------------------------------------------------------------- #
def _three_invoice_chunk(bad_middle_total=False):
    rows = [_row(1, 1, "A1"), _row(1, 2, "A2"), _row(2, 1, "A3"),     # INV-A pages 1-2
            _row(3, 1, "B1"),                                          # INV-B page 3
            _row(4, 1, "C1"), _row(5, 1, "C2")]                        # INV-C pages 4-5
    subs = [
        {"invoice_number_raw": "INV-A", "invoice_date_raw": "01.04.2026", "first_page_no": 1,
         "currency_raw": "USD", "totals": {"grand_total_raw": "300.00"}},
        {"invoice_number_raw": "INV-B", "invoice_date_raw": "02.04.2026", "first_page_no": 3,
         "totals": {"grand_total_raw": "1,000.00" if bad_middle_total else "100.00"}},
        {"invoice_number_raw": "INV-C", "invoice_date_raw": "03.04.2026", "first_page_no": 4,
         "totals": {"grand_total_raw": "200.00"}},
    ]
    return _chunk(rows, subs=subs,
                  header={"invoice_number_raw": "INV-A", "currency_raw": "USD"})


def test_multi_invoice_split_preserves_item_order_and_numbers():
    inv = finalize_invoices([_three_invoice_chunk()])
    assert [i.description_raw for i in inv.items] == ["A1", "A2", "A3", "B1", "C1", "C2"]
    assert [i.xml_item_sequence for i in inv.items] == [1, 2, 3, 4, 5, 6]
    assert [i.source_invoice_number for i in inv.items] == \
        ["INV-A", "INV-A", "INV-A", "INV-B", "INV-C", "INV-C"]
    # per-invoice item index restarts inside each bundled invoice
    assert [i.source_invoice_item_index for i in inv.items] == [1, 2, 3, 1, 1, 2]
    assert [(r.number, r.item_count) for r in inv.invoice_refs] == \
        [("INV-A", 3), ("INV-B", 1), ("INV-C", 2)]
    assert inv.goods_total == Decimal("600.00")
    # no printed combined total -> the sub-invoices' own totals sum stands in
    assert inv.printed_grand_total == Decimal("600.00")
    codes = [w.code for w in inv.warnings]
    assert "ROWS_INCOMPLETE_SUSPECT" not in codes and "TOTAL_MISMATCH" not in codes


def test_multi_invoice_totals_checked_per_invoice():
    inv = finalize_invoices([_three_invoice_chunk(bad_middle_total=True)])
    suspects = [w for w in inv.warnings if w.code == "ROWS_INCOMPLETE_SUSPECT"]
    assert len(suspects) == 1
    assert "invoice INV-B" in suspects[0].message          # names the failing invoice
    assert "1000.00" in suspects[0].message


def test_rows_before_first_sub_invoice_are_kept_with_warning():
    rows = [_row(1, 1, "EARLY"), _row(2, 1, "A1")]
    subs = [{"invoice_number_raw": "INV-A", "first_page_no": 2}]
    inv = finalize_invoices([_chunk(rows, subs=subs)])
    assert [i.description_raw for i in inv.items] == ["EARLY", "A1"]   # nothing dropped
    assert any(w.code == "SUBINVOICE_ROWS_BEFORE_FIRST" for w in inv.warnings)


def test_unanchored_sub_invoice_warns_and_is_ignored():
    rows = [_row(1, 1, "A1")]
    subs = [{"invoice_number_raw": "INV-A", "first_page_no": 1},
            {"invoice_number_raw": "GHOST-9"}]                # no first_page_no
    inv = finalize_invoices([_chunk(rows, subs=subs)])
    assert [r.number for r in inv.invoice_refs] == ["INV-A"]
    assert any(w.code == "SUBINVOICE_UNANCHORED" for w in inv.warnings)


def test_proforma_sub_invoice_excluded_from_goods():
    rows = [_row(1, 1, "REAL"), _row(2, 1, "QUOTE-ROW")]
    subs = [{"invoice_number_raw": "INV-A", "first_page_no": 1,
             "totals": {"grand_total_raw": "100.00"}},
            {"invoice_number_raw": "PF-1", "invoice_kind_raw": "PROFORMA", "first_page_no": 2}]
    inv = finalize_invoices([_chunk(rows, subs=subs)])
    assert [i.description_raw for i in inv.items] == ["REAL"]
    assert any(w.code == "SUBINVOICE_PROFORMA_SKIPPED" for w in inv.warnings)
    assert [r.number for r in inv.invoice_refs] == ["INV-A"]


def test_single_invoice_chunk_behavior_unchanged():
    rows = [_row(1, 1, "A1", total="960.00")]
    chunk = _chunk(rows, header={"invoice_number_raw": "DEMO-209-1", "currency_raw": "USD"},
                   totals={"grand_total_raw": "9,600.00"})    # 10x actual -> row loss alarm
    inv = finalize_invoices([chunk])
    assert [r.number for r in inv.invoice_refs] == ["DEMO-209-1"]
    suspects = [w for w in inv.warnings if w.code == "ROWS_INCOMPLETE_SUSPECT"]
    assert len(suspects) == 1 and "the invoice prints" in suspects[0].message
    assert inv.printed_grand_total == Decimal("9600.00")


def test_combined_grand_total_cross_checked_against_sub_totals():
    base = _three_invoice_chunk()
    chunk = _chunk(
        [r.model_dump() for r in base.rows],
        subs=[s.model_dump() for s in base.sub_invoices],
        totals={"grand_total_raw": "999.00"},                 # printed combined, disagrees with 600
    )
    inv = finalize_invoices([chunk])
    assert any(w.code == "SUBINVOICE_TOTALS_SUM_MISMATCH" for w in inv.warnings)
    assert inv.printed_grand_total == Decimal("999.00")       # printed combined total wins


# --------------------------------------------------------------------------- #
# chunked extraction end-to-end: windows contribute rows AND sub_invoices
# --------------------------------------------------------------------------- #
class _FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.messages_seen = []

    def create(self, **kw):
        self.messages_seen.append(kw["messages"])
        content = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        msg = type("M", (), {"content": content})()
        choice = type("C", (), {"message": msg})()
        return type("R", (), {"choices": [choice]})()


class _FakeClient:
    def __init__(self, responses):
        self.chat = type("Chat", (), {"completions": _FakeCompletions(responses)})()


def test_chunked_multi_invoice_extraction_merges_subs(monkeypatch):
    from app import config as config_mod
    settings = config_mod.get_settings_uncached()
    settings.extraction_chunk_page_threshold = 2
    settings.extraction_chunk_page_size = 2
    settings.deterministic_table_parser_enabled = False
    # call-order fake client -> sequential windows for deterministic mapping
    settings.llm_concurrency = 1
    monkeypatch.setattr("app.extraction.openai_extractor.get_settings", lambda: settings)

    win1 = json.dumps({
        "role_validation": ROLE_OK, "page_numbers": [1, 2],
        "header": {"invoice_number_raw": "INV-A", "currency_raw": "USD"},
        "rows": [_row(1, 1, "A1"), _row(2, 1, "A2")],
        "sub_invoices": [{"invoice_number_raw": "INV-A", "first_page_no": 1}],
    })
    win2 = json.dumps({
        "role_validation": ROLE_OK, "page_numbers": [3, 4],
        "rows": [_row(3, 1, "B1"), _row(4, 1, "B2")],
        "sub_invoices": [
            {"invoice_number_raw": "INV-A",               # totals page of invoice A
             "totals": {"grand_total_raw": "200.00"}},
            {"invoice_number_raw": "INV-B", "first_page_no": 3,
             "totals": {"grand_total_raw": "200.00"}},
        ],
    })
    page = "|  MODEL | DESC | 1 | EA | 100.00 | 100.00  |"
    ocr = OcrDocument(document_id="d", declared_role=DeclaredRole.INVOICE,
                      pages=[OcrPage(page_no=i, plain_text=page) for i in (1, 2, 3, 4)])
    ex = OpenAIExtractor(client=_FakeClient([win1, win2]))
    payload, warnings = ex.extract(DeclaredRole.INVOICE, ocr)
    assert ex._client.chat.completions.calls == 2
    assert [r.description_raw for r in payload.rows] == ["A1", "A2", "B1", "B2"]
    assert [(s.invoice_number_raw, s.first_page_no) for s in payload.sub_invoices] == \
        [("INV-A", 1), ("INV-B", 3)]
    assert payload.sub_invoices[0].totals.grand_total_raw == "200.00"   # merged across windows
    # the window prompts actually ask for sub_invoices
    sys_or_user = [m["content"] for msgs in ex._client.chat.completions.messages_seen
                   for m in msgs]
    assert any("sub_invoices entry" in c for c in sys_or_user)

    inv = finalize_invoices([payload])
    assert [(r.number, r.item_count) for r in inv.invoice_refs] == [("INV-A", 2), ("INV-B", 2)]
    assert inv.printed_grand_total == Decimal("400.00")


# --------------------------------------------------------------------------- #
# header-zone view for the parser-first document-level call
# --------------------------------------------------------------------------- #
def test_header_zone_pages_trim_middle_pages_only():
    # content-aware: a middle page with no header/totals hints keeps only the
    # top/bottom margins; first and last pages stay whole.
    long_text = "\n".join(f"line {i}" for i in range(1, 61))
    pages = [OcrPage(page_no=n, plain_text=long_text) for n in (1, 2, 3)]
    zoned = _header_zone_pages(pages, margin=5, ctx=1)
    assert zoned[0].plain_text == long_text                    # first page whole
    assert zoned[2].plain_text == long_text                    # last page whole
    mid = zoned[1].plain_text.splitlines()
    assert mid[:5] == [f"line {i}" for i in range(1, 6)]       # top margin kept
    assert mid[-5:] == [f"line {i}" for i in range(56, 61)]    # bottom margin kept
    assert "[... omitted ...]" in zoned[1].plain_text          # goods bulk dropped
    assert len(mid) < 60
    short = [OcrPage(page_no=n, plain_text="a\nb") for n in (1, 2, 3)]
    assert all(z.plain_text == "a\nb" for z in _header_zone_pages(short))
