"""Parallel window extraction (2026-07-17 speed work).

Windows of a long document run through a thread pool; the LLM gate
(EASYCUSTOMS_LLM_CONCURRENCY) stays the global token-rate throttle.  These
tests pin: correct window→response routing under ANY thread scheduling,
document order surviving out-of-order window completion, gate sizing from
settings, and the explicit OpenAI client timeout configuration.
"""
import json
import threading
import time

from app.domain.enums import DeclaredRole
from app.extraction import openai_extractor as oe
from app.extraction.openai_extractor import OpenAIExtractor
from app.ocr.base import OcrDocument, OcrPage

ROLE_OK = {"expected_role": "INVOICE", "matches_expected_role": True}


def _row(page, idx, desc):
    return {"source_page_no": page, "source_row_index": idx, "description_raw": desc,
            "quantity_raw": "1", "uom_raw": "EA", "unit_price_raw": "100.00",
            "line_total_raw": "100.00"}


class _RoutedCompletions:
    """Response chosen by request CONTENT (window page range in the note), not
    call order — deterministic no matter which thread calls first.  A route
    may carry a delay so completion order differs from submission order."""

    def __init__(self, routes):
        self._routes = routes                    # substring -> (delay_s, response_json)
        self._lock = threading.Lock()
        self.calls = 0
        self.max_in_flight = 0
        self._in_flight = 0

    def create(self, **kw):
        with self._lock:
            self.calls += 1
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            user = " ".join(m["content"] for m in kw["messages"] if m["role"] == "user")
            for key, (delay, content) in self._routes.items():
                if key in user:
                    if delay:
                        time.sleep(delay)
                    msg = type("M", (), {"content": content})()
                    choice = type("C", (), {"message": msg})()
                    return type("R", (), {"choices": [choice]})()
            raise AssertionError("no route matched request: " + user[:160])
        finally:
            with self._lock:
                self._in_flight -= 1


class _RoutedClient:
    def __init__(self, routes):
        self.chat = type("Chat", (), {"completions": _RoutedCompletions(routes)})()


def test_parallel_windows_keep_document_order(monkeypatch):
    from app import config as config_mod
    settings = config_mod.get_settings_uncached()
    settings.extraction_chunk_page_threshold = 2
    settings.extraction_chunk_page_size = 2
    settings.deterministic_table_parser_enabled = False
    settings.llm_concurrency = 2
    monkeypatch.setattr("app.extraction.openai_extractor.get_settings", lambda: settings)
    monkeypatch.setattr(oe, "_LLM_GATE", None)   # rebuild the gate from these settings

    win1 = json.dumps({"role_validation": ROLE_OK, "page_numbers": [1, 2],
                       "header": {"invoice_number_raw": "INV-1"},
                       "rows": [_row(1, 1, "A1"), _row(2, 1, "A2")]})
    win2 = json.dumps({"role_validation": ROLE_OK, "page_numbers": [3, 4],
                       "rows": [_row(3, 1, "B1"), _row(4, 1, "B2")],
                       "totals": {"grand_total_raw": "400.00"}})
    # window 1 is SLOW: window 2 completes first; merged output must still be
    # in document order with header/totals correctly attributed
    client = _RoutedClient({"pages 1-2": (0.15, win1), "pages 3-4": (0.0, win2)})
    page = "|  MODEL | DESC | 1 | EA | 100.00 | 100.00  |"
    ocr = OcrDocument(document_id="d", declared_role=DeclaredRole.INVOICE,
                      pages=[OcrPage(page_no=i, plain_text=page) for i in (1, 2, 3, 4)])

    payload, warnings = OpenAIExtractor(client=client).extract(DeclaredRole.INVOICE, ocr)
    assert [r.description_raw for r in payload.rows] == ["A1", "A2", "B1", "B2"]
    assert payload.header.invoice_number_raw == "INV-1"
    assert payload.totals.grand_total_raw == "400.00"
    comp = client.chat.completions
    assert comp.calls == 2
    assert comp.max_in_flight == 2               # the two windows truly overlapped


def test_llm_gate_sized_from_settings(monkeypatch):
    from app import config as config_mod
    settings = config_mod.get_settings_uncached()
    settings.llm_concurrency = 3
    monkeypatch.setattr("app.extraction.openai_extractor.get_settings", lambda: settings)
    monkeypatch.setattr(oe, "_LLM_GATE", None)
    gate = oe._llm_gate()
    assert gate._value == 3                      # BoundedSemaphore initial slots
    monkeypatch.setattr(oe, "_LLM_GATE", None)   # leave no cross-test residue


def test_openai_client_gets_timeout_and_no_sdk_retries(monkeypatch):
    from app import config as config_mod
    settings = config_mod.get_settings_uncached()
    settings.llm_api_key = "sk-test-not-real"
    settings.llm_timeout_seconds = 180.0
    monkeypatch.setattr("app.extraction.openai_extractor.get_settings", lambda: settings)
    ex = OpenAIExtractor()                       # builds a real client object, no network
    assert ex._client.max_retries == 0
    assert float(ex._client.timeout) == 180.0
