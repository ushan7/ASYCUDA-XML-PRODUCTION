"""A packing extraction that runs out of budget keeps what it already has.

The old behaviour was all-or-nothing: one late window raised, every sibling
window's result was discarded with it, and the pipeline allocated the whole
shipment by invoice VALUE share.  That is the "long wait, then a proportional
split" this change exists to end — and it threw away rows that had already
been extracted and paid for.

PARTIAL is deliberately a THIRD state.  Treating it as "packing list present"
would silently switch the un-extracted items from the quantity share to the
value share, which systematically under-weights a cheap bulky line.
"""
import json
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.domain.enums import DeclaredRole
from app.extraction import openai_extractor as oe
from app.extraction import service as extraction_service
from app.extraction.openai_extractor import ExtractionDeadlineExceeded, OpenAIExtractor
from app.ocr.base import OcrDocument, OcrPage
from app.rules.models import WorkItem
from app.rules.packing_match import PackingEvidence
from app.rules.weight_carton import allocate_weights_and_cartons

D = Decimal
ROLE_OK = {"expected_role": "PACKING_LIST", "matches_expected_role": True}


def _prow(page, desc):
    return {"source_page_no": page, "source_row_index": 1, "description_raw": desc,
            "gross_weight": {"value_raw": "10", "unit_raw": "KG"}}


class _Slow:
    """First call sleeps past the deadline, then answers; later calls find the
    deadline already gone and abort before reaching the network."""

    def __init__(self, content, delay):
        self.content = content
        self.delay = delay
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        time.sleep(self.delay)
        msg = type("M", (), {"content": self.content})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()


class _Client:
    def __init__(self, content, delay):
        self.chat = type("Chat", (), {"completions": _Slow(content, delay)})()


def _pin(monkeypatch, **over):
    from app import config as config_mod
    s = config_mod.get_settings_uncached()
    s.deterministic_table_parser_enabled = False
    s.extraction_chunk_page_threshold = 2
    s.extraction_chunk_page_size_packing = 2
    s.llm_concurrency = 1                    # sequential, so the clock is deterministic
    s.openai_reasoning_fallback_model = None
    for k, v in over.items():
        setattr(s, k, v)
    monkeypatch.setattr("app.extraction.openai_extractor.get_settings", lambda: s)
    monkeypatch.setattr(oe, "_LLM_GATE", None)
    return s


def _ocr(pages=4):
    return OcrDocument(document_id="d", declared_role=DeclaredRole.PACKING_LIST,
                       pages=[OcrPage(page_no=i + 1, plain_text="| A | 1 | EA |")
                              for i in range(pages)])


def test_a_late_window_no_longer_discards_the_windows_that_finished(monkeypatch):
    _pin(monkeypatch)
    content = json.dumps({"role_validation": ROLE_OK, "rows": [_prow(1, "A1"), _prow(2, "A2")]})
    ex = OpenAIExtractor(client=_Client(content, delay=0.25))
    payload, warnings = ex.extract(DeclaredRole.PACKING_LIST, _ocr(),
                                   deadline=time.monotonic() + 0.15)
    assert [r.description_raw for r in payload.rows] == ["A1", "A2"]      # window 1 survived
    assert any("PACKING_EXTRACTION_PARTIAL" in w for w in warnings)
    assert payload.page_complete is False


def test_losing_every_window_is_still_a_clean_abort(monkeypatch):
    _pin(monkeypatch)
    content = json.dumps({"role_validation": ROLE_OK, "rows": []})
    ex = OpenAIExtractor(client=_Client(content, delay=0))
    with pytest.raises(ExtractionDeadlineExceeded):
        ex.extract(DeclaredRole.PACKING_LIST, _ocr(), deadline=time.monotonic() - 1)


def test_extract_document_keeps_a_partial_payload(monkeypatch):
    """The abort branch builds an EMPTY payload; a partial result must not go
    down it — it goes through locale stamping and validation like any other."""
    content = json.dumps({"role_validation": ROLE_OK, "rows": [_prow(1, "A1")],
                          "warnings": ["PACKING_EXTRACTION_PARTIAL: 1 window(s) hit the budget"]})

    def _run(role, ocr, fixture, deadline=None):
        from app.extraction.common_models import PackingListChunkRaw
        return (PackingListChunkRaw.model_validate(json.loads(content)),
                ["PACKING_EXTRACTION_PARTIAL: 1 window(s) hit the budget"], "openai")

    monkeypatch.setattr(extraction_service, "_run_provider", _run)
    result = extraction_service.extract_document(DeclaredRole.PACKING_LIST, _ocr(2))
    assert len(result.payload.rows) == 1
    assert any("PACKING_EXTRACTION_PARTIAL" in w for w in result.warnings)
    assert result.review_required is True


# --------------------------------------------------------------------------- #
# Allocation: PARTIAL is not "present"
# --------------------------------------------------------------------------- #
def _item(seq, desc, qty, total):
    return WorkItem(
        xml_item_sequence=seq, source_invoice_number="INV-1", source_invoice_date="",
        source_invoice_item_index=seq, source_invoice_item_no=None,
        description_raw=desc, quantity=D(qty), invoice_uom_raw="PCS",
        unit_price=D("1"), line_total=D(total), currency="USD")


def _partial_case(partial):
    # ALPHA was extracted (10 kg of the 100 kg shipment); BETA was not reached.
    # BETA is cheap and bulky: 9 pieces for 10 dollars against ALPHA's 1 piece
    # for 90 — the case where value share and quantity share disagree loudly.
    items = [_item(1, "ALPHA", "1", "90"), _item(2, "BETA", "9", "10")]
    evidence = {1: PackingEvidence(gross_weight=D("10"), matched=True, matched_name="ALPHA"),
                2: PackingEvidence()}
    msgs = allocate_weights_and_cartons(items, evidence, D("100"), D("10"),
                                        packing_present=True, packing_partial=partial)
    return items, msgs


def test_unreached_items_take_the_quantity_share_not_the_value_share():
    items, _ = _partial_case(partial=True)
    # ALPHA keeps its extracted 10 kg per piece; BETA is estimated at the same
    # kg-per-piece density (9 x 10 = 90), not at its 10-dollar value share.
    assert items[1].gross_weight_kg == D("90.0000")
    assert "quantity" in items[1].allocation_audit["gross_weight_source"]


def test_a_complete_packing_list_still_uses_the_value_share_for_unmatched_items():
    items, _ = _partial_case(partial=False)
    assert "invoice value" in items[1].allocation_audit["gross_weight_source"]
    assert items[1].gross_weight_kg != D("90.0000")


def test_the_unmatched_warning_names_the_budget_not_a_wording_mismatch():
    _, msgs = _partial_case(partial=True)
    note = next(m.message for m in msgs if m.code == "PACKING_ITEMS_UNMATCHED")
    assert "time budget" in note and "not found on the packing list" not in note


def test_extracted_rows_are_still_used_as_real_evidence_when_partial():
    items, _ = _partial_case(partial=True)
    assert items[0].allocation_audit["gross_weight_source"] == "packing-list gross weight"


# --------------------------------------------------------------------------- #
# Complete-but-late is not over budget
# --------------------------------------------------------------------------- #
def test_a_slow_but_complete_extraction_keeps_its_evidence():
    """This used to stamp the over-budget marker, which made the pipeline drop
    a fully extracted, fully validated packing list for finishing a few seconds
    late — and split the shipment by invoice value instead."""
    from app.services import packing_timing_note

    note = packing_timing_note(431.0, 240.0, [])
    assert note is not None
    assert "PACKING_EXTRACTION_SLOW" in note
    assert "PACKING_EXTRACTION_OVER_BUDGET" not in note   # the pipeline must NOT drop evidence


def test_a_timing_note_is_not_added_on_time():
    from app.services import packing_timing_note
    assert packing_timing_note(100.0, 240.0, []) is None


@pytest.mark.parametrize("existing", ["PACKING_EXTRACTION_OVER_BUDGET: aborted",
                                      "PACKING_EXTRACTION_PARTIAL: 1 window(s)"])
def test_the_extractors_own_outcome_is_not_overwritten(existing):
    from app.services import packing_timing_note
    assert packing_timing_note(431.0, 240.0, [existing]) is None
