"""Regression tests for the 2026-07-17 sindhu failure: a 21-page invoice was
extracted, the validator flagged a few rows, and the repair resend contained
ONLY the flagged rows — 6 rows survived out of ~75.  These tests pin the three
defenses: corrupted-number parsing, the repair row-loss guard, and the
PAGE_ROWS_MISSING completeness validator (plus window merging).
"""
import json

from decimal import Decimal

from app.domain.enums import DeclaredRole
from app.extraction.common_models import InvoiceChunkRaw, PackingListChunkRaw
from app.extraction.openai_extractor import OpenAIExtractor, _merge_chunk_payloads
from app.extraction.validator import validate_invoice
from app.numbers import parse_decimal
from app.ocr.base import OcrDocument, OcrPage


# --------------------------------------------------------------------------- #
# parse_decimal — OCR-corrupted European formats
# --------------------------------------------------------------------------- #
def test_parse_decimal_dot_corrupted_european():
    assert parse_decimal("1.600.00") == Decimal("1600.00")
    assert parse_decimal("12.345.678.90") == Decimal("12345678.90")


def test_parse_decimal_comma_corrupted_european():
    assert parse_decimal("1,600,00") == Decimal("1600.00")


def test_parse_decimal_unambiguous_dot_thousands():
    assert parse_decimal("1.600.000") == Decimal("1600000")


def test_parse_decimal_space_thousands_still_works():
    assert parse_decimal("USD 1 234,56") == Decimal("1234.56")


def test_parse_decimal_rejects_two_jammed_numbers():
    # merged OCR table cell "unit price + line total" must not concatenate
    assert parse_decimal("35,00 700,00") is None
    assert parse_decimal("320.00 1.600.00") is None


def test_parse_date_composite_swift_printout():
    from datetime import date
    from app.dates import parse_date
    assert parse_date("260405 2026 Apr 05") == date(2026, 4, 5)
    assert parse_date("260405") == date(2026, 4, 5)
    assert parse_date("2026 Apr 05") == date(2026, 4, 5)
    # disagreeing fragments stay unparsed (conservative)
    assert parse_date("260405 2026 Apr 09") is None


def test_parse_decimal_existing_formats_unchanged():
    assert parse_decimal("1,234.56") == Decimal("1234.56")
    assert parse_decimal("1.234,56") == Decimal("1234.56")
    assert parse_decimal("#7023,17#") == Decimal("7023.17")
    assert parse_decimal("(1,200)") == Decimal("-1200")
    assert parse_decimal("1.600") == Decimal("1.600")  # single dot stays decimal


# --------------------------------------------------------------------------- #
# PAGE_ROWS_MISSING completeness validator
# --------------------------------------------------------------------------- #
def _invoice_payload(rows):
    return InvoiceChunkRaw.model_validate({
        "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
        "rows": rows,
    })


ROW_PAGE_1 = {"source_page_no": 1, "source_row_index": 1,
              "description_raw": "STENT RONYX22515X", "quantity_raw": "3",
              "uom_raw": "EA", "unit_price_raw": "320.00", "line_total_raw": "960.00"}

GOODS_PAGE = "|  RONYX22515X | STENT ... | 3 | EA | 320.00 | 960.00  |"
TEXT_PAGE = "PLEASE QUOTE INVOICE NUMBER. TOTAL (USD) | 89,975.52"


def test_page_rows_missing_flags_uncovered_goods_page():
    payload = _invoice_payload([ROW_PAGE_1])
    errors = validate_invoice(payload, {1: GOODS_PAGE, 2: GOODS_PAGE})
    assert any("PAGE_ROWS_MISSING: page 2" in e for e in errors)
    assert not any("page 1" in e for e in errors)


def test_page_rows_missing_ignores_non_goods_pages():
    payload = _invoice_payload([ROW_PAGE_1])
    errors = validate_invoice(payload, {1: GOODS_PAGE, 2: TEXT_PAGE})
    assert not any("PAGE_ROWS_MISSING" in e for e in errors)


# --------------------------------------------------------------------------- #
# Repair row-loss guard
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


def _inv_json(n_rows, bad_total_rows=(), page_no=1):
    rows = []
    for i in range(1, n_rows + 1):
        rows.append({
            "source_page_no": page_no, "source_row_index": i,
            "description_raw": f"STENT MODEL{i}", "quantity_raw": "3", "uom_raw": "EA",
            "unit_price_raw": "320.00",
            "line_total_raw": "1.600.00" if i in bad_total_rows else "960.00",
        })
    return json.dumps({
        "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
        "page_numbers": [page_no], "rows": rows,
    })


def _inv_ocr(n_pages=1):
    page = "|  MODEL | DESC | 3 | EA | 320.00 | 960.00  |"
    return OcrDocument(document_id="d", declared_role=DeclaredRole.INVOICE,
                       pages=[OcrPage(page_no=i + 1, plain_text=page) for i in range(n_pages)])


def test_repair_resend_may_not_drop_rows():
    # round 1: 10 rows, none flag (all parse) -> accepted immediately; force a
    # flagged round instead: use an evidence error via a bogus page reference
    bad = json.dumps({
        "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
        "page_numbers": [1],
        "rows": json.loads(_inv_json(10))["rows"][:10],
        "warnings": [],
    })
    # make row 3 carry an unparseable total so round 1 fails validation
    b = json.loads(bad)
    b["rows"][2]["line_total_raw"] = "35,00 700,00"
    round1 = json.dumps(b)
    round2_subset = _inv_json(1)         # model resends ONLY the corrected row
    round3_full = _inv_json(10)          # after the guard fires, full resend
    ex = OpenAIExtractor(client=_FakeClient([round1, round2_subset, round3_full]))
    ex._max_rounds = 3
    payload, warnings = ex.extract(DeclaredRole.INVOICE, _inv_ocr())
    assert len(payload.rows) == 10
    comp = ex._client.chat.completions
    assert comp.calls == 3
    # the guard message was actually sent
    assert any("REPAIR_DROPPED_ROWS" in m["content"]
               for msgs in comp.messages_seen for m in msgs if m["role"] == "user")


def test_exhausted_rounds_returns_most_complete_payload():
    b = json.loads(_inv_json(10))
    b["rows"][0]["line_total_raw"] = "35,00 700,00"   # never fixed
    always_bad_full = json.dumps(b)
    subset = _inv_json(2)
    ex = OpenAIExtractor(client=_FakeClient([always_bad_full, subset, subset]))
    ex._max_rounds = 2
    payload, warnings = ex.extract(DeclaredRole.INVOICE, _inv_ocr())
    assert len(payload.rows) == 10       # best parse, not the last subset
    assert any("FIELD_REVIEW_REQUIRED" in w for w in warnings)


# --------------------------------------------------------------------------- #
# Chunked extraction + merge
# --------------------------------------------------------------------------- #
def test_long_invoice_is_extracted_in_page_windows_and_merged(monkeypatch):
    from app import config as config_mod
    settings = config_mod.get_settings_uncached()
    settings.extraction_chunk_page_threshold = 2
    settings.extraction_chunk_page_size = 2
    # this test pins the chunked-LLM path; keep the deterministic parser (and
    # any populated vendor-layout store) out of the way
    settings.deterministic_table_parser_enabled = False
    # the fake client maps responses by CALL ORDER — run windows sequentially
    # so window N deterministically receives response N
    settings.llm_concurrency = 1
    monkeypatch.setattr("app.extraction.openai_extractor.get_settings", lambda: settings)

    # 4 pages -> 2 windows of 2; each response covers its window's pages
    win1 = json.dumps({
        "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
        "page_numbers": [1, 2],
        "header": {"invoice_number_raw": "INV-1"},
        "rows": [dict(json.loads(_inv_json(1, page_no=1))["rows"][0]),
                 dict(json.loads(_inv_json(1, page_no=2))["rows"][0])],
    })
    win2 = json.dumps({
        "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
        "page_numbers": [3, 4],
        "rows": [dict(json.loads(_inv_json(1, page_no=3))["rows"][0]),
                 dict(json.loads(_inv_json(1, page_no=4))["rows"][0])],
        "totals": {"grand_total_raw": "3,840.00"},
    })
    ex = OpenAIExtractor(client=_FakeClient([win1, win2]))
    payload, warnings = ex.extract(DeclaredRole.INVOICE, _inv_ocr(4))
    assert ex._client.chat.completions.calls == 2
    assert len(payload.rows) == 4
    assert payload.page_numbers == [1, 2, 3, 4]
    assert payload.header.invoice_number_raw == "INV-1"
    assert payload.totals.grand_total_raw == "3,840.00"


def test_awb_freight_prorated_to_authority_weight():
    """User rule 2026-07-17: a consolidated MAWB's freight is offered as the
    HAWB-weight-proportional candidate (MAWB total x authority/MAWB kg)."""
    from types import SimpleNamespace
    from app.pipeline import ResolvedContext, freight_candidates

    ctx = ResolvedContext(
        inv=SimpleNamespace(currency="SGD"), ship=SimpleNamespace(), banking=SimpleNamespace(),
        items=[], packing_evidence={},
        invoice_freight=None, awb_freight=Decimal("1647.66"), banking_freight=Decimal("1200"),
        exchange_rate=Decimal("151.01"),
        awb_freight_detail="MASTER_AIR_WAYBILL freight 3427.88 SGD covers 387 kg; prorated to authority 186 kg = 1647.66",
        awb_freight_currency="SGD",
    )
    cands = freight_candidates(ctx)
    assert [c["source"] for c in cands] == ["BANKING_SWIFT", "AWB"]
    assert cands[1]["amount"] == "1647.66"
    assert "prorated" in cands[1]["detail"]
    # same currency as the invoice -> the reviewer may click it straight in
    assert cands[1]["currency"] == "SGD" and cands[1]["comparable"] is True


def _awb_payload(**boxes):
    from app.domain.enums import DeclaredRole as _Role
    from app.extraction.common_models import (
        AirWaybillExtractionRaw, AirWaybillFormRaw, RawMoney, RawNumber, RoleValidation)
    form = AirWaybillFormRaw(
        logical_form_id="m", document_title_raw="Air Waybill",
        document_kind_raw="MASTER_AIR_WAYBILL", primary_awb_number_raw="235-41325852",
        gross_weight=RawNumber(value_raw="236.0", unit_raw="K"),
        chargeable_weight=RawNumber(value_raw="550.0"),
        pieces_or_packages=RawNumber(value_raw="2"),
        **{k: RawMoney(amount_raw=v, currency_raw="EUR") for k, v in boxes.items() if v})
    return AirWaybillExtractionRaw(
        role_validation=RoleValidation(expected_role=_Role.AIR_WAYBILL), forms=[form])


def test_awb_freight_candidate_is_total_prepaid_not_weight_charge():
    """Reference AWB 235-41325852 (samples/max/awb.pdf): freight is the
    EUR 4708.00 Total Prepaid box (4653.00 weight charge + 55.00 AWC), and the
    authority weight equals the waybill's 236 kg so nothing is prorated away.
    The system was declaring the 4653.00 weight charge (user report
    2026-07-21)."""
    from app.pipeline import awb_freight_candidate

    warnings = []
    amount, detail, currency = awb_freight_candidate(
        [_awb_payload(total_prepaid="4,708.00", weight_charge="4653.00",
                      other_charges_total="55.00", freight_amount="4708.00")],
        Decimal("236"), warnings)
    assert amount == Decimal("4708.00")
    assert "Total Prepaid 4708.00" in detail and "EUR" in detail and not warnings
    # the waybill's own currency is returned, not just printed into `detail`
    assert currency == "EUR"


def test_awb_freight_candidate_prorates_a_consolidated_master():
    from app.pipeline import awb_freight_candidate

    warnings = []
    amount, detail, currency = awb_freight_candidate(
        [_awb_payload(total_prepaid="4708.00", weight_charge="4653.00",
                      other_charges_total="55.00")],
        Decimal("118"), warnings)                      # HAWB covers half the 236 kg
    assert amount == Decimal("2354.00") and "prorated to authority 118 kg" in detail
    assert currency == "EUR"


def test_merge_packing_chunks():
    a = PackingListChunkRaw.model_validate({
        "role_validation": {"expected_role": "PACKING_LIST", "matches_expected_role": True},
        "packing_list_number_raw": None,
        "invoice_references_raw": ["INV-1"],
        "rows": [{"source_page_no": 1, "source_row_index": 1, "description_raw": "CATH A",
                  "quantity_raw": "10"}],
    })
    b = PackingListChunkRaw.model_validate({
        "role_validation": {"expected_role": "PACKING_LIST", "matches_expected_role": True},
        "packing_list_number_raw": "PL-9",
        "invoice_references_raw": ["INV-1", "INV-2"],
        "rows": [{"source_page_no": 2, "source_row_index": 1, "description_raw": "CATH B",
                  "quantity_raw": "2"}],
        "total_gross_weight": {"value_raw": "186", "unit_raw": "KG"},
    })
    merged = _merge_chunk_payloads(DeclaredRole.PACKING_LIST, [a, b])
    assert len(merged.rows) == 2
    assert merged.packing_list_number_raw == "PL-9"
    assert merged.invoice_references_raw == ["INV-1", "INV-2"]
    assert merged.total_gross_weight.value_raw == "186"
