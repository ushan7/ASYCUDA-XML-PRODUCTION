"""Vendor layouts that merge quantity+UOM into one cell ("25 EA (1/EA)") —
the Abbott "INV ALL7" format that made the table parser stand down on
2026-07-17 — plus the fast-model tier for row windows.

Pins: parser confirmation (with free HS/COO enrichment from the material
cell), manifest anchors on digit-led part numbers, PAGE_ROWS_MISSING on
merged-cell pages, and mini-first/escalate-on-repair model routing.
"""
import json
import threading

from app.domain.enums import DeclaredRole
from app.extraction.manifest import goods_row_anchors
from app.extraction.openai_extractor import OpenAIExtractor
from app.extraction.table_parser import parse_pages
from app.extraction.validator import validate_invoice
from app.extraction.common_models import InvoiceChunkRaw
from app.ocr.base import OcrDocument, OcrPage

HEADER = "|  Line | Material | Material Description | Qty UoM (Size) | Unit Price | Extended Price | GST  |"
SEP = "| --- | --- | --- | --- | --- | --- | --- |"
ROW1 = ("|  10 | 01E3120 (Qty) Batch/Expiry Date (25) 69698UQ01/25.Jan.2028 Country of Origin JP "
        "Tariff Code 38220090 | MLTGNT DIRECT LDL | 25 EA (1/EA) | 222.20 | 5,555.00 | N  |")
ROW2 = ("|  20 | 01E4604 (Qty) Batch/Expiry Date (2) 63085UN26/23.May.2027 Country of Origin US "
        "Tariff Code 38220090 | ICT SERUM CAL(10X10) | 2 EA (1/EA) | 100.00 | 200.00 | N  |")
ROW_BAD_MATH = ("|  30 | 01E4921 (Qty) Batch/Expiry Date (5) 07416UN25/31.Mar.2027 Country of Origin US "
                "| ICT INT REF(2X2L) | 5 EA (1/EA) | 54.90 | 999.99 | N  |")


def test_parser_confirms_merged_qty_uom_rows_with_hs_and_coo():
    page = "\n".join([HEADER, SEP, ROW1, ROW2])
    res = parse_pages(DeclaredRole.INVOICE, {1: page}, {1: "US"})
    pp = res.pages[1]
    assert pp.confirmed and len(pp.rows) == 2
    r = pp.rows[0]
    assert r.line_no_raw == "10"
    assert r.model_raw == "01E3120"
    assert r.description_raw == "MLTGNT DIRECT LDL"
    assert (r.quantity_raw, r.uom_raw) == ("25", "EA")
    assert (r.unit_price_raw, r.line_total_raw) == ("222.20", "5,555.00")
    assert r.hs_code_raw == "38220090"          # free deterministic enrichment
    assert r.country_of_origin_raw == "JP"


def test_parser_disowns_page_on_merged_row_failing_arithmetic():
    page = "\n".join([HEADER, SEP, ROW1, ROW_BAD_MATH])   # 5 x 54.90 != 999.99
    res = parse_pages(DeclaredRole.INVOICE, {1: page}, {1: "US"})
    pp = res.pages[1]
    assert not pp.confirmed and pp.suspicious_leftover >= 1


def test_manifest_anchors_digit_led_parts_in_merged_cells():
    anchors = goods_row_anchors(1, "\n".join([HEADER, SEP, ROW1]))
    assert len(anchors) == 1
    assert "01E3120" in anchors[0].tokens         # digit-led part number anchors the row


def test_page_rows_missing_fires_for_merged_qty_pages():
    payload = InvoiceChunkRaw.model_validate(
        {"role_validation": {"expected_role": "INVOICE", "matches_expected_role": True}, "rows": []})
    errors = validate_invoice(payload, {1: "\n".join([HEADER, SEP, ROW1])})
    assert any("PAGE_ROWS_MISSING: page 1" in e for e in errors)


# --------------------------------------------------------------------------- #
# fast-tier routing: mini first on row windows, primary on repair + judgement
# --------------------------------------------------------------------------- #
class _ModelRecordingCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.models_seen = []
        self._lock = threading.Lock()

    def create(self, **kw):
        with self._lock:
            i = min(len(self.models_seen), len(self._responses) - 1)
            self.models_seen.append(kw["model"])
        msg = type("M", (), {"content": self._responses[i]})()
        choice = type("C", (), {"message": msg})()
        return type("R", (), {"choices": [choice]})()


class _ModelRecordingClient:
    def __init__(self, responses):
        self.chat = type("Chat", (), {"completions": _ModelRecordingCompletions(responses)})()


def _settings(monkeypatch, **over):
    from app import config as config_mod
    s = config_mod.get_settings_uncached()
    s.deterministic_table_parser_enabled = False
    s.llm_concurrency = 1
    s.openai_reasoning_enabled = True
    s.openai_reasoning_model = "gpt-primary"
    s.openai_reasoning_fallback_model = "gpt-mini"
    for k, v in over.items():
        setattr(s, k, v)
    monkeypatch.setattr("app.extraction.openai_extractor.get_settings", lambda: s)
    return s


def _row(page, idx, desc, total="100.00", **kw):
    return {"source_page_no": page, "source_row_index": idx, "description_raw": desc,
            "quantity_raw": "1", "uom_raw": "EA", "unit_price_raw": total,
            "line_total_raw": total, **kw}


def test_row_window_uses_mini_then_escalates_to_primary(monkeypatch):
    _settings(monkeypatch, extraction_chunk_page_threshold=1, extraction_chunk_page_size=4)
    bad = json.dumps({"role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
                      "page_numbers": [1, 2],
                      "rows": [_row(1, 1, "A1", total="35,00 700,00")]})   # unparseable -> repair
    good = json.dumps({"role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
                       "page_numbers": [1, 2], "rows": [_row(1, 1, "A1"), _row(2, 1, "B1")]})
    ocr = OcrDocument(document_id="d", declared_role=DeclaredRole.INVOICE,
                      pages=[OcrPage(page_no=n, plain_text="no tables here") for n in (1, 2)])
    ex = OpenAIExtractor(client=_ModelRecordingClient([bad, good]))
    payload, warnings = ex.extract(DeclaredRole.INVOICE, ocr)
    assert ex._client.chat.completions.models_seen == ["gpt-mini", "gpt-primary"]
    assert len(payload.rows) == 2
    assert any("WINDOW_ESCALATED" in w for w in warnings)


def test_whole_document_judgement_calls_never_use_mini(monkeypatch):
    _settings(monkeypatch)
    ok = json.dumps({"role_validation": {"expected_role": "BANKING", "matches_expected_role": True},
                     "sender_bic_raw": "CTZNNPKAXXX"})
    ocr = OcrDocument(document_id="d", declared_role=DeclaredRole.BANKING,
                      pages=[OcrPage(page_no=1, plain_text="SWIFT FIN 700. Sender CTZNNPKAXXX.")])
    ex = OpenAIExtractor(client=_ModelRecordingClient([ok]))
    ex.extract(DeclaredRole.BANKING, ocr)
    assert ex._client.chat.completions.models_seen == ["gpt-primary"]


def test_resolved_fast_llm_model_placeholder_handling():
    from app import config as config_mod
    s = config_mod.get_settings_uncached()
    s.openai_reasoning_fallback_model = "  gpt-5.4-mini "
    assert s.resolved_fast_llm_model() == "gpt-5.4-mini"
    s.openai_reasoning_fallback_model = "your_model_here"
    assert s.resolved_fast_llm_model() is None
    s.openai_reasoning_fallback_model = None
    assert s.resolved_fast_llm_model() is None
