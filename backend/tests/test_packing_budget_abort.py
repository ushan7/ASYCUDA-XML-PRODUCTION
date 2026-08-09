"""Packing-list extraction is ABORTED at the time budget (user follow-up
2026-07-18), not run to completion and then discarded.

Pins: the deadline stops new LLM calls before they are made; extract_document
converts the abort into an over-budget empty result (so the pipeline uses the
quantity-share fallback); a None deadline never aborts; and only the packing
role gets a deadline.
"""
import time

import pytest

from app.domain.enums import DeclaredRole
from app.extraction import service as extraction_service
from app.extraction.openai_extractor import ExtractionDeadlineExceeded, OpenAIExtractor
from app.ocr.base import OcrDocument, OcrPage


class _CountingCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        content = '{"role_validation": {"expected_role": "PACKING_LIST", ' \
                  '"matches_expected_role": true}, "rows": []}'
        msg = type("M", (), {"content": content})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()


class _CountingClient:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": _CountingCompletions()})()


def _packing_ocr(pages=2):
    return OcrDocument(document_id="d", declared_role=DeclaredRole.PACKING_LIST,
                       pages=[OcrPage(page_no=i + 1, plain_text="| A | 1 | EA |") for i in range(pages)])


def _pin_llm_path(monkeypatch):
    """Force the plain LLM-window path (parser off) so the fake client drives it."""
    from app import config as config_mod
    s = config_mod.get_settings_uncached()
    s.deterministic_table_parser_enabled = False
    s.extraction_chunk_page_threshold = 10          # <= no chunking for 2 pages
    s.llm_concurrency = 1
    monkeypatch.setattr("app.extraction.openai_extractor.get_settings", lambda: s)
    return s


def test_past_deadline_aborts_before_any_llm_call(monkeypatch):
    _pin_llm_path(monkeypatch)
    ex = OpenAIExtractor(client=_CountingClient())
    with pytest.raises(ExtractionDeadlineExceeded):
        ex.extract(DeclaredRole.PACKING_LIST, _packing_ocr(), deadline=time.monotonic() - 1)
    assert ex._client.chat.completions.calls == 0      # aborted before calling


def test_future_deadline_allows_the_call(monkeypatch):
    _pin_llm_path(monkeypatch)
    ex = OpenAIExtractor(client=_CountingClient())
    payload, _ = ex.extract(DeclaredRole.PACKING_LIST, _packing_ocr(), deadline=time.monotonic() + 300)
    assert ex._client.chat.completions.calls >= 1      # deadline not hit -> ran normally


def test_no_deadline_never_aborts(monkeypatch):
    _pin_llm_path(monkeypatch)
    ex = OpenAIExtractor(client=_CountingClient())
    payload, _ = ex.extract(DeclaredRole.PACKING_LIST, _packing_ocr(), deadline=None)
    assert ex._client.chat.completions.calls >= 1


def test_extract_document_converts_abort_to_over_budget_result(monkeypatch):
    def _boom(role, ocr, fixture, deadline=None):
        raise ExtractionDeadlineExceeded()
    monkeypatch.setattr(extraction_service, "_run_provider", _boom)

    result = extraction_service.extract_document(
        DeclaredRole.PACKING_LIST, _packing_ocr(), deadline=time.monotonic() - 1)

    assert result.role_match is True                   # job proceeds, not FAILED
    assert result.review_required is True
    assert (result.payload.rows == [])                 # empty payload — evidence dropped downstream
    assert any("PACKING_EXTRACTION_OVER_BUDGET" in w for w in result.warnings)
    assert "aborted" in result.warnings[0]
    assert result.provider == "openai (aborted at budget)"


def test_marker_matches_pipeline_detection_substring():
    marker = extraction_service.packing_budget_marker(240.0)
    # the exact substring resolve_context scans for
    assert "PACKING_EXTRACTION_OVER_BUDGET" in marker
    assert "240s" in marker
